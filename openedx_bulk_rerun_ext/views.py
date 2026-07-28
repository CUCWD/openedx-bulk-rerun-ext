"""
Views for openedx_bulk_rerun_ext.
"""
import logging
import threading

from django.db import close_old_connections, transaction
from django.db.models import Prefetch
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BulkRerunBatch, CourseRerunJob, CourseRerunLog, CourseRerunSettings, CourseRerunTeamMember
from .permissions import IsSuperuser
from .serializers import (
    BulkRerunBatchCreateSerializer,
    BulkRerunBatchSerializer,
    BulkRerunBatchStatusSerializer,
    BulkRerunBatchSummarySerializer,
    CourseRerunJobSerializer,
    CourseRerunLogSerializer,
    CreateCourseRerunJobSerializer,
    ValidateKeysSerializer,
)

# Jobs in these statuses own the target_course_key; a new job cannot claim
# the same target until an existing one has failed (and thus released the slot).
_ACTIVE_STATUSES = [
    CourseRerunJob.Status.PENDING,
    CourseRerunJob.Status.RUNNING,
    CourseRerunJob.Status.SUCCEEDED,
]

# For the validate endpoint's DB pass we only treat PENDING/RUNNING as a
# reservation — the key is actively being written to.  SUCCEEDED is intentionally
# excluded: the modulestore check (Pass 2) is the authoritative source of truth
# for whether the course still exists, so a SUCCEEDED row for a since-deleted
# course must not be flagged as a conflict.
_RESERVED_STATUSES = [
    CourseRerunJob.Status.PENDING,
    CourseRerunJob.Status.RUNNING,
]


def _dispatch_rollback_in_background(batch_id_str):
    """
    Start dispatch_batch_rollback without blocking the HTTP response.

    Mirrors the batch-creation dispatch pattern: a daemon thread (or Celery,
    per BULK_RERUN_USE_CELERY inside _dispatch_task) runs the rollback while
    the view returns immediately and the UI polls rollback_status.
    """
    from .tasks import _dispatch_task, dispatch_batch_rollback  # pylint: disable=import-outside-toplevel

    log = logging.getLogger(__name__)

    def _run():
        close_old_connections()
        try:
            _dispatch_task(dispatch_batch_rollback, batch_id_str)
        except Exception:  # pylint: disable=broad-exception-caught
            log.exception('[BulkRerun] Rollback dispatch failed for batch %s', batch_id_str)
            try:
                BulkRerunBatch.objects.filter(id=batch_id_str).update(
                    rollback_status=BulkRerunBatch.RollbackStatus.FAILED,
                )
            except Exception:  # pragma: no cover  # pylint: disable=broad-exception-caught
                pass
        finally:
            close_old_connections()

    transaction.on_commit(
        lambda: threading.Thread(target=_run, daemon=True).start()
    )


# ── Phase 1 views ─────────────────────────────────────────────────────────────


class BulkRerunAccessView(APIView):
    """Return whether the requesting user may access bulk reruns."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Expose the user's exact Django superuser status to Authoring."""
        return Response({'is_superuser': request.user.is_superuser})


class ValidateCourseKeysView(APIView):
    """
    Check a list of target course keys for conflicts before bulk submission.

    POST /api/bulk-rerun/validate/ — accepts a list of target course keys and
    returns the subset that already exist, checking both active CourseRerunJob
    rows and the platform modulestore.  Used by the UI on each debounce cycle
    to surface conflicts before submission.
    """

    permission_classes = [IsSuperuser]

    def post(self, request):
        """Validate a list of target course keys and return those that already exist."""
        # Deserialize and enforce min=1 / max=500 list constraints.
        serializer = ValidateKeysSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        keys = serializer.validated_data['keys']

        # Reject the entire request early if any key is malformed.
        # CourseKey.from_string() is the authoritative parser — never use regex
        # against course key strings directly.
        for key_str in keys:
            try:
                CourseKey.from_string(key_str)
            except InvalidKeyError:
                return Response(
                    {'error': f'Invalid course key: {key_str}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Pass 1: check our own DB for jobs that are actively reserving a target
        # key (PENDING or RUNNING).  SUCCEEDED is excluded here because the
        # modulestore check in Pass 2 is the authoritative source of truth — a
        # SUCCEEDED row for a since-deleted course must not be flagged as a
        # conflict.  Dry-run jobs are excluded: they write DB rows but never
        # create real courses, so their target keys must not be flagged either.
        existing = set(
            CourseRerunJob.objects.filter(
                target_course_key__in=keys,
                status__in=_RESERVED_STATUSES,
            ).exclude(
                batch__is_dry_run=True,
            ).values_list('target_course_key', flat=True)
        )

        # Pass 2: check the platform modulestore for courses that exist
        # independently of our job table (e.g. manually created courses or
        # reruns initiated through the normal Studio UI).
        # xmodule is unavailable in test environments that run without a
        # full edx-platform install; skip the modulestore check gracefully.
        try:
            from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel,import-error
            store = modulestore()
            for key_str in keys:
                # Skip keys already found in Pass 1 to avoid redundant lookups.
                if key_str not in existing:
                    if store.has_course(CourseKey.from_string(key_str)):
                        existing.add(key_str)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # Return only the subset of input keys that already exist.
        # The UI uses this list to highlight conflicts before the user submits.
        return Response({'existing': list(existing)})


class CourseRerunJobListCreate(APIView):
    """
    List and create individual rerun jobs.

    GET /api/bulk-rerun/jobs/ — list jobs owned by the current user.

    POST /api/bulk-rerun/jobs/ — create a single rerun job and dispatch
    the Celery task immediately.

    Accepts an optional ``?bulk_job_id=<uuid>`` query param on GET to filter
    jobs belonging to the same bulk submission.
    """

    permission_classes = [IsSuperuser]

    def get(self, request):
        """Return all rerun jobs owned by the requesting user, newest first."""
        # Scope to the requesting user so staff cannot see each other's jobs.
        jobs = CourseRerunJob.objects.filter(created_by=request.user)

        # Optional filter: return only jobs belonging to a single bulk submission.
        bulk_job_id = request.query_params.get('bulk_job_id')
        if bulk_job_id:
            jobs = jobs.filter(bulk_job_id=bulk_job_id)

        return Response(CourseRerunJobSerializer(jobs, many=True).data)

    def post(self, request):
        """Create a rerun job and immediately dispatch the Celery task."""
        # Deserialize input fields; job_type defaults to "individual" and
        # bulk_job_id defaults to None when not provided.
        serializer = CreateCourseRerunJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source_key_str = data['source_course_key']
        target_key_str = data['target_course_key']

        # Validate that both keys are parseable before touching the DB or
        # modulestore. InvalidKeyError means the string is not a valid
        # course key format (e.g. missing org, course, or run segments).
        try:
            CourseKey.from_string(source_key_str)
            CourseKey.from_string(target_key_str)
        except InvalidKeyError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # A rerun that points at itself would silently overwrite the source course.
        if source_key_str == target_key_str:
            return Response(
                {'error': 'source_course_key and target_course_key must differ'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Confirm the source course actually exists in the modulestore before
        # creating a job. Without this check the Celery task would fail with
        # an ItemNotFoundError deep inside clone_course, which is harder to debug.
        # xmodule is unavailable outside the platform; skip gracefully if so.
        try:
            from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel,import-error
            if not modulestore().has_course(CourseKey.from_string(source_key_str)):
                return Response(
                    {'error': f'Source course does not exist: {source_key_str}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # For existing-org job types, verify the target org is registered on the
        # platform before creating the job. new_org skips this check because org
        # registration (Phase 0) is a prerequisite that happens outside this flow —
        # the org won't exist yet by design.
        if data['job_type'] != CourseRerunJob.JobType.NEW_ORG:
            try:
                # pylint: disable=import-outside-toplevel,import-error
                from organizations.api import get_organization_by_short_name
                from organizations.exceptions import InvalidOrganizationException
                target_org = CourseKey.from_string(target_key_str).org
                try:
                    get_organization_by_short_name(target_org)
                except InvalidOrganizationException:
                    return Response(
                        {
                            'error': (
                                f'Organization "{target_org}" is not registered on this platform. '
                                'Register it via organizations.api.add_organization() before running a rerun.'
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        # Prevent duplicate jobs for the same target. A failed job releases the
        # slot (it is not in _ACTIVE_STATUSES) so a retry submission is allowed.
        if CourseRerunJob.objects.filter(
            target_course_key=target_key_str,
            status__in=_ACTIVE_STATUSES,
        ).exists():
            return Response(
                {'error': 'An active job for this target_course_key already exists'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # All validation passed — create the DB row in "pending" status.
        job = CourseRerunJob.objects.create(
            source_course_key=source_key_str,
            target_course_key=target_key_str,
            job_type=data['job_type'],
            bulk_job_id=data['bulk_job_id'],
            created_by=request.user,
        )

        # Dispatch the Celery task and store its ID so operators can trace it
        # in Flower or the Django admin. The task transitions the job through
        # running → succeeded/failed asynchronously.
        from .tasks import _dispatch_task, run_course_rerun  # pylint: disable=import-outside-toplevel
        result = _dispatch_task(run_course_rerun, str(job.id))
        job.celery_task_id = result.id
        job.save(update_fields=['celery_task_id'])

        # Return 201 with the full job record so the client can begin polling
        # GET /api/bulk-rerun/jobs/{id}/ for status updates.
        return Response(CourseRerunJobSerializer(job).data, status=status.HTTP_201_CREATED)


class CourseRerunJobDetail(APIView):
    """
    Return the current state of a single CourseRerunJob.

    GET /api/bulk-rerun/jobs/<uuid:job_id>/ — returns 404 if the job does not
    exist or was not created by the requesting user.
    """

    permission_classes = [IsSuperuser]

    def get(self, request, job_id):
        """Return the current state of a single rerun job; 404 if not owned by the caller."""
        try:
            job = CourseRerunJob.objects.get(id=job_id)
        except CourseRerunJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        # Return 404 rather than 403 to avoid leaking that the job exists
        # to a user who did not create it.
        if job.created_by != request.user:
            return Response(status=status.HTTP_404_NOT_FOUND)

        return Response(CourseRerunJobSerializer(job).data)


# ── Phase 2 views ─────────────────────────────────────────────────────────────

class BulkRerunBatchListCreateView(APIView):
    """
    List or create bulk rerun batches.

    GET  /api/bulk-rerun/batches/           — lists ALL users' batches, so the
         Tracking Progress page shows a shared view of every operator's runs
         (each entry carries created_by_username for the UI's user filter).
         ?status=running,pending             — optional comma-separated filter.
         Returns the lightweight summary serializer (no nested job logs) so the
         Tracking Progress page can recover in-flight batches on page load or
         cross-device without relying on client-side localStorage.

    POST /api/bulk-rerun/batches/           — creates one BulkRerunBatch and N
         CourseRerunJob rows, then dispatches the fan-out Celery task.
         Accepts an optional ``config_snapshot`` field that is stored in
         ``BulkRerunBatch.config_json`` for later recovery via the GET endpoint.
         Returns 202 Accepted with the batch ID and initial job list.
    """

    permission_classes = [IsSuperuser]

    def get(self, request):
        """List all users' batches; ?status= filters by comma-separated status values."""
        qs = BulkRerunBatch.objects.all()
        status_param = request.query_params.get('status')
        if status_param:
            qs = qs.filter(status__in=[s.strip() for s in status_param.split(',')])
        qs = qs.order_by('-created_at')[:100]
        return Response(BulkRerunBatchSummarySerializer(qs, many=True).data)

    def post(self, request):
        """Validate the batch payload, create all rows, and dispatch the fan-out task."""
        serializer = BulkRerunBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Reject keys with an in-flight job (PENDING or RUNNING only).
        # SUCCEEDED is excluded — existing courses are allowed; the task will
        # skip creation and re-apply settings to the already-present course.
        # Dry-run jobs are excluded: they write DB rows but never create real courses.
        target_keys = [c['target_course_key'] for c in data['courses']]
        blocked = set(
            CourseRerunJob.objects.filter(
                target_course_key__in=target_keys,
                status__in=_RESERVED_STATUSES,
            ).exclude(
                batch__is_dry_run=True,
            ).values_list('target_course_key', flat=True)
        )

        # Pass 2: for target keys with a SUCCEEDED job, check whether the course
        # still exists in the modulestore.  A live course means the slot is taken —
        # block the submission so the operator is aware before proceeding.
        candidate_keys = [k for k in target_keys if k not in blocked]
        if candidate_keys:
            succeeded_keys = set(
                CourseRerunJob.objects.filter(
                    target_course_key__in=candidate_keys,
                    status=CourseRerunJob.Status.SUCCEEDED,
                ).exclude(
                    batch__is_dry_run=True,
                ).values_list('target_course_key', flat=True)
            )
            if succeeded_keys:
                try:
                    # pylint: disable=import-outside-toplevel,import-error
                    from xmodule.modulestore.django import modulestore
                    store = modulestore()
                    for key_str in succeeded_keys:
                        if store.has_course(CourseKey.from_string(key_str)):
                            blocked.add(key_str)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

        if blocked:
            return Response(
                {'error': 'Active jobs already exist for these target keys', 'keys': sorted(blocked)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Remove any dry-run job rows for these target keys so they do not
        # interfere with the real job rows created below.
        CourseRerunJob.objects.filter(
            target_course_key__in=target_keys,
            batch__is_dry_run=True,
        ).delete()

        batch = BulkRerunBatch.objects.create(
            created_by=request.user,
            mode=data['mode'],
            is_dry_run=data['is_dry_run'],
            target_run=data['target_run'],
            prog_id=data.get('prog_id', ''),
            config_json=request.data.get('config_snapshot') or None,
        )

        jobs = []
        for position, course in enumerate(data['courses']):
            job = CourseRerunJob.objects.create(
                batch=batch,
                position=position,
                source_course_key=course['source_course_key'],
                target_course_key=course['target_course_key'],
                job_type=course['job_type'],
                created_by=request.user,
            )
            jobs.append(job)

        # Create the settings row from the nested payload.
        settings_data = data['settings']
        CourseRerunSettings.objects.create(batch=batch, **settings_data)

        # Create one team member row per CAR entry (may be empty list).
        # Deduplicate by (org, email) — the same person legitimately appears in
        # multiple org rosters with different org values; only exact (org, email)
        # duplicates within the same batch payload are dropped.
        seen: set = set()
        for member in data.get('team_members', []):
            email = (member.get('email') or '').strip()
            org = (member.get('org') or '').strip()
            if not email or (org, email) in seen:
                continue
            seen.add((org, email))
            CourseRerunTeamMember.objects.create(batch=batch, **member)

        from .tasks import _dispatch_task, dispatch_batch_rerun  # pylint: disable=import-outside-toplevel

        log = logging.getLogger(__name__)
        batch_id_str = str(batch.id)

        def _dispatch():
            # Runs the full task chain in a daemon thread so the 202 response
            # returns immediately and the UI can poll for real-time progress.
            # _dispatch_task uses BULK_RERUN_USE_CELERY (default False) to decide
            # whether to run synchronously here or enqueue to a Celery worker.
            close_old_connections()
            try:
                _dispatch_task(dispatch_batch_rerun, batch_id_str)
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception('[BulkRerun] Background dispatch thread failed for batch %s', batch_id_str)
                from .models import BulkRerunBatch as _Batch  # pylint: disable=import-outside-toplevel
                try:
                    _Batch.objects.filter(id=batch_id_str).update(status=_Batch.Status.FAILED)
                except Exception:  # pragma: no cover  # pylint: disable=broad-exception-caught
                    pass
            finally:
                close_old_connections()

        # transaction.on_commit ensures the thread starts AFTER the current
        # DB transaction commits, so dispatch_batch_rerun can see the batch
        # and job rows. Without this, MySQL REPEATABLE READ isolation can
        # cause the task to get DoesNotExist and silently leave all jobs pending.
        transaction.on_commit(
            lambda: threading.Thread(target=_dispatch, daemon=True).start()
        )

        return Response(
            {
                'batch_id':   str(batch.id),
                'status':     batch.status,
                'total_jobs': len(jobs),
                'jobs': [
                    {
                        'id':                str(j.id),
                        'target_course_key': j.target_course_key,
                        'status':            j.status,
                    }
                    for j in jobs
                ],
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BulkRerunBatchDetailView(APIView):
    """
    Return the full status of a batch including per-course job state.

    GET /api/bulk-rerun/batches/<uuid:batch_id>/ — polled by the UI Track
    Progress screen.  Visible to any authenticated user (the tracking page is
    a shared view of every operator's batches); returns 404 only if the batch
    does not exist.

    Supports ``?include_logs=false`` to omit each job's nested log lines,
    keeping the polling payload constant-size for long-running batches.  The
    UI polls with include_logs=false and fetches log lines incrementally via
    CourseRerunJobLogsView; the full (default) response is used for one-shot
    hydration on page load/refresh and for the history view.
    """

    permission_classes = [IsSuperuser]

    def get(self, request, batch_id):
        """Return batch status and the nested job list; 404 if the batch does not exist."""
        include_logs = request.query_params.get('include_logs', 'true').lower() != 'false'

        # Jobs MUST come back in position order so the UI can match them to
        # courseItems by index or by target_course_key reliably.
        # The model's default ordering (-created_at) would return jobs in
        # reverse creation order, silently assigning wrong logs to wrong courses.
        # Logs are ordered by created_at via CourseRerunLog.Meta.ordering.
        jobs_queryset = CourseRerunJob.objects.order_by('position')
        if include_logs:
            jobs_queryset = jobs_queryset.prefetch_related(
                Prefetch('logs', queryset=CourseRerunLog.objects.order_by('created_at'))
            )

        try:
            batch = BulkRerunBatch.objects.prefetch_related(
                Prefetch('jobs', queryset=jobs_queryset)
            ).get(id=batch_id)
        except BulkRerunBatch.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer_class = BulkRerunBatchSerializer if include_logs else BulkRerunBatchStatusSerializer
        return Response(serializer_class(batch).data)


class BulkRerunBatchCancelView(APIView):
    """
    Cancel a bulk rerun batch that is stuck or no longer wanted.

    POST /api/bulk-rerun/batches/<uuid:batch_id>/cancel/ — marks all
    pending/running jobs as failed and sets the batch status to failed.
    Any authenticated user may cancel any batch — the tracking page is a
    shared view, so a cancel button rendered on another operator's batch
    must work rather than 404.
    Accepts an optional ``{"rollback": true}`` body — when set, a rollback
    pass is dispatched after cancellation that deletes every course the
    batch created so far (jobs with course_created=True).  Jobs still
    mid-clone when cancel lands are covered by the _finalize_job race
    guard, which deletes their course the moment they finish.
    Returns 400 if the batch is already in a terminal state.
    Returns 404 if the batch does not exist.
    """

    permission_classes = [IsSuperuser]

    def post(self, request, batch_id):
        """Cancel a non-terminal batch; optionally roll back the courses it created."""
        try:
            batch = BulkRerunBatch.objects.get(id=batch_id)
        except BulkRerunBatch.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        terminal = {
            BulkRerunBatch.Status.SUCCEEDED,
            BulkRerunBatch.Status.FAILED,
            BulkRerunBatch.Status.PARTIAL,
        }
        if batch.status in terminal:
            return Response(
                {'error': 'Batch is already in a terminal state and cannot be cancelled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rollback = bool(request.data.get('rollback')) and not batch.is_dry_run

        from django.utils import timezone as tz  # pylint: disable=import-outside-toplevel
        now = tz.now()
        cancelled = batch.jobs.filter(
            status__in=[CourseRerunJob.Status.PENDING, CourseRerunJob.Status.RUNNING],
        ).update(
            status=CourseRerunJob.Status.FAILED,
            error_message='Cancelled by user.',
            completed_at=now,
        )
        batch.status = BulkRerunBatch.Status.FAILED
        batch.completed_at = now
        update_fields = ['status', 'completed_at']

        if rollback:
            # PENDING must be set BEFORE the response so the _finalize_job race
            # guard sees rollback_requested even if a mid-clone job finishes
            # between this request returning and the rollback task starting.
            batch.rollback_status = BulkRerunBatch.RollbackStatus.PENDING
            update_fields.append('rollback_status')
        batch.save(update_fields=update_fields)

        if rollback:
            _dispatch_rollback_in_background(str(batch.id))

        return Response({
            'cancelled_jobs': cancelled,
            'status': batch.status,
            'rollback_status': batch.rollback_status,
        })


class BulkRerunBatchRollbackView(APIView):
    """
    Roll back a terminal batch by deleting every course it created.

    POST /api/bulk-rerun/batches/<uuid:batch_id>/rollback/ — dispatches the
    rollback task and returns 202; the UI polls rollback_status on the batch.
    Used from the History tab when a mistake is noticed after the run
    finished (the Stop button covers the mid-run case via cancel's
    ``rollback`` flag).

    Only courses with course_created=True are deleted — targets the batch
    adopted rather than created are never touched.  Deletion is permanent.

    Returns 400 when the batch is not terminal, is a dry run, has already
    had a rollback requested, or created no courses.
    Returns 404 if the batch does not exist.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, batch_id):
        """Validate eligibility and dispatch the rollback task; 202 on success."""
        try:
            batch = BulkRerunBatch.objects.get(id=batch_id)
        except BulkRerunBatch.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if not batch.is_terminal:
            return Response(
                {'error': 'Batch is still running — use cancel with rollback instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if batch.is_dry_run:
            return Response(
                {'error': 'Dry-run batches create no courses; there is nothing to roll back.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if batch.rollback_requested:
            return Response(
                {'error': f'A rollback was already requested (status: {batch.rollback_status}).'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        eligible = batch.jobs.filter(course_created=True, rolled_back=False).count()
        if eligible == 0:
            return Response(
                {'error': 'This batch has no rollback-eligible courses on record. '
                          'Batches run before rollback support cannot be rolled back automatically.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.rollback_status = BulkRerunBatch.RollbackStatus.PENDING
        batch.save(update_fields=['rollback_status'])
        _dispatch_rollback_in_background(str(batch.id))

        return Response(
            {
                'batch_id': str(batch.id),
                'rollback_status': batch.rollback_status,
                'eligible_courses': eligible,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class CourseRerunJobLogsView(APIView):
    """
    Return structured log lines for a single CourseRerunJob.

    GET /api/bulk-rerun/jobs/<uuid:job_id>/logs/ — returns all log lines.
    Supports ``?since=<id>`` for incremental polling; only lines with
    id > since are returned, avoiding re-fetching the full history.
    Visible to any authenticated user so the shared tracking page can stream
    logs for batches started by other operators.
    Returns 404 if the job does not exist.
    """

    permission_classes = [IsSuperuser]

    def get(self, request, job_id):
        """Return log lines for a job, optionally filtered to those newer than ?since=<id>."""
        try:
            job = CourseRerunJob.objects.get(id=job_id)
        except CourseRerunJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        logs = job.logs.all()
        since = request.query_params.get('since')
        if since:
            try:
                logs = logs.filter(id__gt=int(since))
            except (TypeError, ValueError):
                return Response(
                    {'error': 'since must be an integer log line ID'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response({
            'job_id':     str(job.id),
            'job_status': job.status,
            'logs':       CourseRerunLogSerializer(logs, many=True).data,
        })
