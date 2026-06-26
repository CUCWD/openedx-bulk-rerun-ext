# Course re-run tool 🔨

---

## What is **openedx-bulk-rerun-ext?**

### The problem

When an organization runs a course on Open edX, they often need to run it again — for a new semester, a new cohort of learners, or a new partner institution. Open edX calls this a **course rerun**: making a copy of an existing course so it can be delivered again with new dates and a fresh set of learners.

Out of the box, Open edX only supports doing this **one course at a time** through Studio, its course authoring tool. For small operations this is manageable. But many organizations — particularly those running structured programs made up of many courses — need to rerun **ten, twenty, or even fifty course at once**, every semester. Doing that one by one through studio means:

- Clicking through the same screens dozens of times
- Manually entering start and end dates for every course
- Manually adding the same team member to every course
- Manually configuring certificates on every course
- Spending hours on what should be a routine operational task
- High risk of inconsistency or missed steps across courses

### The solution

`openedx-bulk-rerun-ext` is an extension to Open edX’s Studio that adds **bulk course rerun capabilities.** Instead of repeating the same steps for each course individually, an operator submits a single request that specifies:

- **Which courses** to rerun and what their new course IDs should be
- **Scheduling** — start dates, end dates, enrollments windows, and pacing (instructor-led or self-paced)
- **Certificates** — what type of certificate to offer and how to configure it
- **Team access** — which staff members should have access to all the new courses, and in what roles
- **Lesson gating** — whether prerequisite rules from the original course should carry over

The system then handles everything automatically in the background — creating all the course shells, applying all the settings to each one, and reporting back in real time so the operator can see exactly what’s happening and catch any issues as they occur.

### Key Concepts

**Course rerun** — A copy of an existing course, created so it can be delivered to a new group of learners. The content and structure are inherited from the original; dates, enrollment, and team access are configured fresh.

**Batch** — A single bulk submission. One batch can contain anywhere from one to two hundred course reruns, all sharing the same settings and team configuration

**Dry run** — A simulation mode where the system goes through all the steps and logs exactly what it would do, without actually creating or changing anything. Useful for verifying a setup before committing to it.

**Track Progress** — A real-time view that shows the status of every course in a batch as its being processed — which ones are complete, which are still running, and if anything went wrong, exactly what failed and why.

---

### Where the code runs

This is not a standalone service. It’s a Django app that plugs into edx-platform’s CMS process via the Open edX plugin framework. On CMS boot:

- Its URLs get mounted at `/api/bulk-rerun/` under the `cms.django` namespace
- Its settings module `settings/common.py` merges into CMS settings

---

### Phase 1 endpoints (individual jobs)

`POST /validate/` 

Check target course keys for conflicts before submission

- Request: `{ "keys": ["course-v1:org+course+run", ...] }` (1-500 keys)
- Response: `{ "existing": [...] }` — subset of all keys that already exist (active job or modulestore)

`GET /jobs/` 

List the requesting users jobs

- Query param: `?bulk_job_id=<uuid>` (optional filter)
- Response: array of `CourseRerunJobSerializer` (all model fields)

`POST /jobs/` 

Create and immediately dispatch a single rerun job

- Request: `{ "source_course_key": str, "target_course_key": str, "job_type": "individual"|"program_rerun"|"new_org" (default "individual"), "bulk_job_id": uuid|null }`
- Validates: keys parsable, source ≠ target, source exists in modulestore, target org registered (unless `new_org` ), no active job already on target
- Response: `201` + full job board

`GET /jobs/uuid:job_id/` 

Single job status, `404` if not owned by caller

### Phase 2 endpoints (batches)

`POST /batches/`

The main entry point — submits a full batch

- Request:
    
    `{
       "mode": "program_rerun"|"new_org"|"individual",
       "is_dry_run": false,
       "target_run": "2026_T1",
       "prog_id": "",
       "courses": [
          {"source_course_key": "...", "target_course_key": "...", "job_type": "individual"}
    ],
        "settings": {
            "course_start": "...", "course_end": "...",
            "enrollment_start": "...", "enrollment_end": "...",
            "pacing": "instructor"|"self",
            "course_mode": "honor"|"audit"|"verified",
            "cert_display": "early_no_info"|"early_with_info"|"end",
            "create_cert": true, "student_gen_cert": true, "cert_on_dashboard": true,
            "gating_mode": "disabled"|"copy"|"custom",
            "gating_min_score": "80",
            "gating_min_completion": "100",
            "remove_provisioner_after": true
        },
        "team_members": [
            {"org": "MyOrg", "email": "...", "studio_role": "admin"|"staff"|"data_researcher", "discussion_role": "discussion_admin"|"moderator"|"none"}
      ]
    }`
    
- Validates: 1 - 200 courses, no duplicate targets within batch, key validity/source ≠ target per course, scheduling window sanity (`course_start < course_end`, enrollment window inside course window), no active job blocks any target
- Response: `202  Accepted`  + `{ "batch_id", "status", "total_jobs", "jobs": [{id, target_course_key, status}, ...] }`

`GET /batches/`

List the caller's batches (most recent 100), using the lightweight summary serializer (no nested job logs).

- Query param: `?status=running,pending` — optional comma-separated filter
- Response (`BulkRerunBatchSummarySerializer`): `id, status, mode, is_dry_run, target_run, prog_id, created_at, completed_at, created_by_username, config_json`

`GET /batches/<uuid:batch_id>/` 

Full batch status — meant to be polled every 2 secs by the UI

- Response (`BulkRerunBatchSerializer`): `id, status, mode, is_dry_run, target_run, prog_id, total_jobs, done_jobs, failed_jobs, settings_applied_count, phase, created_at, completed_at, jobs[]` — each job nested with `id, position, status, settings_applied, source/target_course_key, started_at, completed_at, elapsed_seconds, error_message, logs[]`
- `phase` is a derived integer: `1` = pending/running (course creation in progress), `4` = terminal (done — UI stops polling and shows export summary)

`POST /batches/<id>/cancel/`

Cancel a non-terminal batch

- Returns `400` if batch is already terminal
- Bulk-updates all `pending/running` jobs to `failed` with `error_message='Cancelled by user` in a single `UPDATE` query
- Sets batch status to `failed` directly
- Response: `{ "cancelled_jobs": <count>, "status": "failed" }`

`GET /jobs/<uuid:job_id>/logs/` 

Structured log tail for one job

- Query param: `?since=<log_id>` for incremental polling (returns only `id > since`)
- Response: `{ "job_id", "job_status", "logs": [{id, level, message, created_at}, ...] }`

---

### Applicators Reference

`applicators.py` contains the post-rerun settings functions for `openedx_bulk_rerun_ext.` Each function applies one category of operator-configured settings to a course that has just been created by OpenEDX’s native `rerun_course` .

**Why does this module exist?** OpenEDX’s built-in rerun only clones a course shell — it copies content and structure, but knows nothing about an organization’s provisioning requirements. Applicators bridge that gap: after the clone succeeds, they run in sequence to configure scheduling, certificates, team access, gating, and org association on the new course.

**Where it fits in the flow:**

```markup
POST /api/bulk-rerun/batches/
        │
        ▼
dispatch_batch_rerun      ← fans out one task per course
        │
        ▼
run_course_rerun          ← calls OpenEdX's rerun_course (the clone)
        │
        ▼
apply_course_settings     ← orchestrates applicators in sequence
        │
        ├── ensure_org_course_association()
        ├── enroll_provisioner()
        ├── apply_scheduling()
        ├── apply_certificates()
        ├── apply_team_access()
        ├── apply_gating()            (only if gating_mode != disabled)
        ├── publish_course()
        └── remove_provisioner()      (only if remove_provisioner_after=True)

```

The Celery task `apply_course_settings` in `tasks.py` is the orchestrator; applicators themselves have no knowledge of Celery, retries, or batch state.

### Structured logging

All status output flows through `_log` at the top of the module:

```python
def _log(job_id, level, message):
    from .models import CourseRerunLog
    CourseRerunLog.objects.create(job_id=job_id, level=level, message=message)
```

| **Level** | **UI Color** | **Meaning** |
| --- | --- | --- |
| `info` | Blue | Step starting; descriptive detail |
| `ok` | Green | Step completed successfully |
| `warn` | Amber | Non-fatal problem; step skipped or degraded |
| `err` | Red | Fatal error written by the task layer |

---

## Models Reference

### Overview

`models.py` defines the five database models that make up the persistence layer for `openedx_bulk_rerun_ext.` Together they form a clear hierarchy: a **batch** groups **jobs**, each job is configured by shared **settings** and **team members**, and each job produces an append-only stream of **log lines**.

**Model hierarchy:**

```markdown
BulkRerunBatch                ← one UI submission
 ├── CourseRerunSettings       ← scheduling, certs, gating config (1:1)
 ├── CourseRerunTeamMember[]   ← who gets access to every course in the batch
 └── CourseRerunJob[]          ← one per source→target course pair
      └── CourseRerunLog[]     ← append-only log lines for the progress UI
```

### `BulkRerunBatch`

Represents a single submission from the bulk rerun UI — one click of ‘Execute reruns”. All jobs created from that submission belong to this batch, and the batch status rolls up from those jobs once they all reach a terminal state.

**Fields**

| **Field** | **Type** | **Description** |
| --- | --- | --- |
| `id` | `UUIDField` | Primary key, auto-generated |
| `created_by` | `FK -> User` | User who submitted the batch; `SET_NULL` on user deletion |
| `created_at`  | `DateTimeField` | Auto-set at creation |
| `completed_at`  | `DateTimeField`  | Set by `_check_batch_completion` when all jobs are terminal; null until then |
| `mode`  | `CharField`  | Origin mode — see `Mode` choices below |
| `is_dry_run`  | `BooleanField`  | If `True` , no platform changes are made; tasks log simulated steps only |
| `target_run`  | `charfield`  | Human-readable run identifier (e.g. `"Fall 2025"`) |
| `prog_id`  | `charfield`  | Program identifier; empty string for non-program batches |
| `status` | `charfield`  | Current lifecycle state — see `Status` choices below; indexed |
| `config_json`  | `JSONField`  | Snapshot of the wizard config object at submission time, used by the progress UI to reconstruct display context after a page refresh |

`Status` choices

| Value | Meaning |
| --- | --- |
| `pending` | Batch created; `dispatch_batch_rerun` has not run yet |
| `running` | At least one job is active |
| `succeeded` | All jobs completed successfully |
| `failed` | All jobs failed |
| `partial` | Mix of successes and failures |

`PARTIAL` is unique to batches — individual jobs only have `succeeded` or `failed`.

`Mode` choices

| Value | Meaning |
| --- | --- |
| `program_rerun` | Rerunning courses within an existing program |
| `new_org` | Onboarding courses into a new organization |
| `individual` | One-off individual course rerun |

### `CourseRerunJob`

Tracks a single source → target course rerun operation. One row is created per target course key at batch submission time, starting in `PENDING` status.

**Fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `UUIDField` | Primary key, auto-generated |
| `batch` | `FK → BulkRerunBatch` | The batch this job belongs to; `SET_NULL` on batch deletion. `null` for Phase 1 jobs submitted individually |
| `position` | `PositiveIntegerField` | Display order within the batch; used by the fan-out task for stagger timing (2s × position) |
| `bulk_job_id` | `UUIDField` | Phase 1 grouping field; kept for backward compatibility. Indexed |
| `created_by` | `FK → User` | User who submitted the job; `SET_NULL` on user deletion |
| `created_at` | `DateTimeField` | Auto-set at creation |
| `started_at` | `DateTimeField` | Set when the Celery task first picks up the job; null until then |
| `completed_at` | `DateTimeField` | Set by `_finalize_job` when the job reaches a terminal state |
| `status` | `CharField` | Current lifecycle state — see `Status` choices below; indexed |
| `job_type` | `CharField` | Listed below |
| `source_course_key` | `CharField` | Course key of the course being cloned |
| `target_course_key` | `CharField` | Course key of the new course to be created; indexed |
| `celery_task_id` | `CharField` | ID of the Celery task running `run_course_rerun`; useful for debugging |
| `error_message` | `TextField` | Populated on failure; empty string on success |
| `settings_applied` | `BooleanField` | Set to `True` by `apply_course_settings` when all applicators have run |

`Status` choices

| Value | Meaning |
| --- | --- |
| `pending` | Job created; Celery task has not started yet |
| `running` | Celery task is actively executing |
| `succeeded` | Course was cloned and all settings applied |
| `failed` | Task exhausted retries or hit an unrecoverable error |

`job_type` choices

| Value | When it's used |
| --- | --- |
| `individual` | Default. A one-off rerun of a single course outside of any program context |
| `program_rerun` | The course is part of a program rerun batch |
| `new_org` | The course is being onboarded into a new organization — skips the org registration check |

**Important:** Only `pending`, `running`, and `succeeded` jobs block a target course key slot. A `failed` job releases the slot, allowing a retry submission with the same target key.

### `CourseRerunSettings`

Stores all operator-configured settings that the applicators apply to every course in a batch after the rerun clone succeeds. One row per `BulkRerunBatch` ; accessed as `batch.settings`.

#### Fields

**Scheduling**

| Field | Type | Description |
| --- | --- | --- |
| `course_start` | `DateTimeField` | Course start date applied to all courses in the batch |
| `course_end` | `DateTimeField` | Course end date |
| `enrollment_start` | `DateTimeField` | Enrollment open date |
| `enrollment_end` | `DateTimeField` | Enrollment close date |
| `pacing` | `CharField` | `instructor` or `self` — see `Pacing` choices |

**Certificates**

| Field | Type | Description |
| --- | --- | --- |
| `course_mode` | `CharField` | Enrollment mode: `honor`, `audit`, or `verified` |
| `cert_display` | `CharField` | When certificates are shown — see `CertDisplay` choices |
| `create_cert` | `BooleanField` | Whether to activate a cert config and enable cert generation |
| `student_gen_cert` | `BooleanField` | Whether learners can self-generate certificates |
| `cert_on_dashboard` | `BooleanField` | Whether certificates appear on the learner dashboard |

**Gating**

| Field | Type | Description |
| --- | --- | --- |
| `gating_mode` | `CharField` | Lesson gating strategy — see `GatingMode` choices |
| `gating_min_score` | `CharField` | Minimum score % a learner must achieve on a subsection before the next unlocks; used by `custom` mode (default `"80"`) |
| `gating_min_completion` | `CharField` | Minimum completion % required before the next subsection unlocks; used by `custom` mode (default `"100"`) |

**Provisioner cleanup**

| Field | Type | Description |
| --- | --- | --- |
| `remove_provisioner_after` | `BooleanField` | If `True`, the submitting user is removed from instructor/staff roles after all other settings are applied |

**Choice enums**

`Pacing` — `instrcutor` (default) or `self`

`CourseMode` — `honor` (default), `audit`, `verified`

`CertDisplay`

| Value | Meaning |
| --- | --- |
| `early_no_info` | Show cert option early, without grade info |
| `early_with_info` | Show cert option early, with grade info |
| `end` | Show cert option only at end of course |

`GatingMode`

| Value | Meaning |
| --- | --- |
| `disabled` | No gating applied (default) |
| `copy` | Copy prerequisite rules from the source course |
| `custom` | Chain every subsection as a sequential prerequisite gate, using `gating_min_score` and `gating_min_completion` thresholds |

### `CourseRerunTeamMember`

One row per person per org listed in the "Team & Access" step of the batch submission UI. Members are scoped to an organization — during provisioning, only members whose `org` matches the course's org are applied to that course.

**Fields**

| Field | Type | Description |
| --- | --- | --- |
| `batch` | `FK → BulkRerunBatch` | The batch this member belongs to; `CASCADE` delete |
| `org` | `CharField` | Organization short name this member belongs to. Empty string = apply to all orgs (legacy behavior) |
| `email` | `EmailField` | Used to look up the platform `User` at provisioning time |
| `studio_role` | `CharField` | Studio course team role — see `StudioRole` choices |
| `discussion_role` | `CharField` | Discussion forum role — see `DiscussionRole` choices |

**Choice enums**

`StudioRoles` — `admin`, `staff`, `data_researcher`

`DiscussionRole` — `discussion_admin`, `moderator`, `none`

**Notes**

- Members are uniquely identified by `(batch, org, email)` — the same person can appear on multiple org rosters within a single batch with different org values
- If a team member's email doesn't exist on the platform at provisioning time, `apply_team_access` logs a warning and skips that member — it does not fail the job
- Each team member is also enrolled in the course using the batch's configured `course_mode`

### `CourseRerunLog`

An append-only structured log line attached to a single `CourseRerunJob` . Written by the Celery task and applicators as the job executes; read by the polling endpoint (`GET /api/bulk-rerun/jobs/<uuid>/logs/`) so the Track Progress UI can display real-time status.

**Fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `BigAutoField` (default) | Sequential integer; used for `?since=<id>` incremental polling |
| `job` | `FK → CourseRerunJob` | The job this log line belongs to; `CASCADE` delete |
| `created_at` | `DateTimeField` | Auto-set at creation; indexed |
| `level` | `CharField` | Severity — see `Level` choices below |
| `message` | `TextField` | Human-readable log message displayed in the UI |

**`Level` choices**

| Value | UI Color | Meaning |
| --- | --- | --- |
| `info` | Blue `#79b8ff` | Step starting; descriptive context |
| `ok` | Green `#4ec994` | Step completed successfully |
| `warn` | Amber `#f0ad4e` | Non-fatal problem; step skipped or degraded |
| `err` | Red `#ff7b72` | Fatal error written by the task layer |

---

## Tasks Reference

### Overview

`tasks.py` is the orchestration layer of `openedx_bulk_rerun_ext` . It contains the Celery tasks and internal helpers that drive a course rerun from start to finish — managing state transitions on the models, fanning out work across jobs, executing the platform clone, and calling the applicators in sequence.

The file does four distinct things:

1. **State** **management** — all status transitions on `BulkRerunBatch` and `CourseRerunJob` live here
2. **Orchestration** — decides what runs, in what order, and with what staggering
3. **Retry logic** — defines retry budgets and guards against duplicate execution
4. **Applicator dispatch** — calls each function from `applicators.py` in the correct sequence

### Dispatch Mode

`_dispatch_task(task, *args)`

Internal helper that controls how tasks are dispatched — either to a real Celery broker or synchronously in the current thread.

### Internal Helpers

`_log(job_id, level, message)`

Appends one `CourseRerunLog` row for the given job. Identical in contract to the `_log` in `applicators.py` — both modules define their own copy so neither has to import from the other.

`_finalize_job(job, success, error='')`

Sets a terminal status on a `CourseRerunJob` and triggers the batch rollup.

**Behaviour**:

1. Sets `job.status` to `SUCCEED` or `FAILED`
2. Stamps `Job.completed_at = timezone.now()`
3. If `success=False` , stores the error string in `job.error_message`
4. Saves only the changed fields via `update_fields` to avoid race conditions with other writers
5. If the job belongs to a batch (`job.batch_id` is set), calls `_check_batch_completion`

`_check_batch_completion(batch_id)`

Rolls up the `BulkRerunBatch` status once all child jobs have reached a terminal state. Called by `_finalize_job` after every job completion.

### Celery Tasks

`dispatch_batch_rerun(batch_id)`

**Type:** `@shared_task` (no retries)

Fan-out task that marks the batch as running and schedules one `run_course_rerun` task per pending job.

**Behaviour**:

1. Fetches the `BulkRerunBatch` ; returns silently if not found
2. Sets `batch.status = RUNNING`
3. Queries all `PENDING` jobs ordered by `position`
4. Calls `_dispatch_task(run_course_rerun, job.id)` for each

`run_course_rerun(self, job_id)`

**Type:** `@shared_task(bind=True, max_retries=3, default_retry_delay=30`

Executes the actual course clone by calling OpenEDX’s native `rerun_course` task, then chains to `apply_course_settings`

`apply_course_settings(self, job_id)`

**Type:** `@shared_task(bind=True, max_retries=2, default_retry_delay=15)`

Applies all operator-configured settings to a newly created course by calling the applicators in sequence. Always called after `run_course_rerun` succeeds.

### Task Chain Summary

```markdown
dispatch_batch_rerun(batch_id)
    └── for each pending job:
        run_course_rerun(job_id)           max 3 retries, 30s delay
            └── apply_course_settings(job_id)  max 2 retries, 15s delay
                    └── _finalize_job()
                            └── _check_batch_completion()
```

---

## Dry Run Mode

One of the most valuable features of the bulk rerun extension is the ability to simulate an entire batch submission before committing to it. This is called a **dry run**.

When an operator submits a batch with dry run enabled, the system goes through every step of the process for every course in the batch — checking course keys, validating settings, simulating scheduling, certificates, team assignments, and gating rules — but makes no actual changes to the platform. No courses are created, no dates are set, no users are granted access. 

Instead, the Track Progress panel displays a detailed log of everything that would happen, step by step, for every course. The operator can read through it and verify:

- All course IDs are valid and don’t conflict with existing courses
- The scheduling dates are correct
- Every team member’s email exists on the platform and would receive the right role
- Certificate and gating settings would be applied as expected

Only once the operator is satisfied that everything looks right do they submit the real batch. 

---

## Lesson Gating

Lesson gating controls whether learner must satisfy a prerequisite before they can access certain content. In a structured program, for example, learner might be required to complete Course 1 before they can enroll in Course 2.

When a course is rerun, its gating rules don’t automatically carry over — they need to be explicitly configured on a new course. The bulk rerun extension handles this as part of the batch settings, giving operators three options:

- **Disabled** — no gating applied (default)
- **Copy from source** — copies both sides of the prerequisite relationship from the source course, translating each subsection's UsageKey into the equivalent block in the new course
- **Custom** — chains every subsection as a sequential prerequisite gate. Subsection B unlocks only after a learner meets the configured `gating_min_score` and `gating_min_completion` thresholds on Subsection A, and so on through the whole course

---

## How to Use the Extension

This section walks through a typical operator workflow — from submitting a batch to monitoring its progress.

### 1. Open the Bulk Rerun tool

Log in to Studio as an admin or staff user. Navigate to the **Bulk Rerun** tool from the Studio admin panel. You’ll land on the batch submission wizard.

### 2. Choose a mode

Select the mode that matches your use case:

| Mode | When to use |
| --- | --- |
| **Program Rerun** | You’re running all courses in an existing program for a new term |
| **New Org** | You’re onboarding courses into a brand-new organization (skips org registration check) |
| **Individual** | One-off reruns outside any program context |

### 3. Add your courses

For each course you want to rerun, provide:

- **Source course key** — the existing course to clone (e.g. `course-v1:MyOrg+CS101+2025_T1`)
- **Target course key** — the new course key to create (e.g. `course-v1:MyOrg+CS101+2026_T1`)

You can add up to 200 courses in a single batch. Use the **Validate** step to check all target keys for conflicts before proceeding — the system will flag any keys that already exist in the platform.

### 4. Configure batch settings

Fill in the shared settings that will be applied to every course in the batch:

- **Scheduling** — set `course_start`, `course_end`, `enrollment_start`, and `enrollment_end`. The enrollment window must fall inside the course window.
- **Pacing** — choose `instructor` (default) or `self`.
- **Certificates** — select the course mode (`honor`, `audit`, or `verified`), when certificates are displayed, and whether learners can self-generate them.
- **Lesson Gating** — choose `disabled` (default), `copy` (inherit prerequisite rules from the source course), or `custom` (chain every subsection sequentially using `gating_min_score` and `gating_min_completion` thresholds).

### 5. Add team members (optional)

In the **Team & Access** step, add any staff who should have access to all new courses. For each person provide:

- Their **org** — the organization short name to scope this member to (e.g. `MyOrg`). Members are only applied to courses whose org matches this value
- Their **email address** (must already have a platform account)
- Their **Studio role** — `admin`, `staff`, or `data_researcher`
- Their **Discussion role** — `discussion_admin`, `moderator`, or `none`

If a member’s email isn’t found on the platform at provisioning time, that member is skipped with a warning — the rest of the batch continues normally.

Enable **Remove provisioner after** if you want the submitting user automatically removed from instructor/staff roles on all new courses once provisioning completes.

### 6. Run a dry run first

Before submitting for real, check the **Dry Run** toggle. A dry run simulates the entire batch — it logs every step that would happen without creating or modifying anything. Open the **Track Progress** panel and read through the output to verify:

- All course IDs are valid and conflict-free
- Scheduling dates are correct
- Every team member’s email resolves to an existing platform account
- Certificate and gating settings would be applied as expected

Only proceed to a real submission once the dry run output looks correct.

### 7. Submit the batch

Uncheck **Dry Run** and click **Execute Reruns**. The system returns immediately with a `202 Accepted` response and begins processing in the background.

### 8. Monitor progress

The **Track Progress** panel polls the batch status every 2 seconds. For each course you’ll see:

- **Status** — `pending`, `running`, `succeeded`, or `failed`
- **Real-time log lines** — color-coded by level (`info`, `ok`, `warn`, `err`)
- **Elapsed time** per course

If a course fails, the `err`-level log lines describe exactly what went wrong. A failed job releases its target key slot, so you can re-submit just that course once the issue is resolved.

### 9. Cancel a batch (if needed)

If you need to stop a batch that is still running, click **Cancel**. This immediately marks all `pending` and `running` jobs as `failed` and sets the batch status to `failed`. Courses that already completed are not rolled back.
