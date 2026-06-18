"""
Database models for openedx_bulk_rerun_ext.
"""
import uuid

from django.conf import settings
from django.db import models


class BulkRerunBatch(models.Model):
    """
    Groups all CourseRerunJob rows submitted together from one UI action.

    One batch = one click of "Execute reruns" or "Onboard organizations".
    Status rolls up from the individual job statuses once all jobs are terminal.

    .. no_pii: Stores job metadata and user FK only; no personal data in fields.
    """

    class Status(models.TextChoices):
        """Lifecycle states for a batch; partial means some jobs failed."""

        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        PARTIAL = 'partial', 'Partial'

    class Mode(models.TextChoices):
        """Origin mode matching CourseRerunJob.JobType categories."""

        PROGRAM_RERUN = 'program_rerun', 'Program Rerun'
        NEW_ORG = 'new_org', 'New Org'
        INDIVIDUAL = 'individual', 'Individual'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='bulk_rerun_batches',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    is_dry_run = models.BooleanField(default=False)
    target_run = models.CharField(max_length=64)
    prog_id = models.CharField(max_length=32, blank=True, default='')
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    config_json = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            'Snapshot of the wizard cfg object (rows, prog, fromMode, etc.) '
            'stored at submission time so the progress UI can reconstruct '
            'display context on any device or after a page refresh.'
        ),
    )

    class Meta:
        """Default ordering for batch listings."""

        ordering = ['-created_at']

    @property
    def total_jobs(self):
        """Return the total number of jobs in this batch."""
        return self.jobs.count()

    @property
    def done_jobs(self):
        """Return the number of terminal jobs (succeeded or failed)."""
        return self.jobs.filter(
            status__in=[CourseRerunJob.Status.SUCCEEDED, CourseRerunJob.Status.FAILED]
        ).count()

    @property
    def failed_jobs(self):
        """Return the number of failed jobs in this batch."""
        return self.jobs.filter(status=CourseRerunJob.Status.FAILED).count()

    @property
    def is_terminal(self):
        """Return True once the batch has reached a final state."""
        return self.status in (
            self.Status.SUCCEEDED, self.Status.FAILED, self.Status.PARTIAL
        )


class CourseRerunJob(models.Model):
    """
    Tracks a single source→target course rerun operation.

    One row is created per target course key. A bulk UI submission creates
    multiple rows that share the same batch so they can be queried and
    displayed together.

    .. no_pii: This model stores job metadata only. User identity is captured
        as a foreign key reference to the auth.User model (handled separately).
        Course keys, task IDs, and error messages contain no personal data.
    """

    class Status(models.TextChoices):
        """Terminal and non-terminal lifecycle states for a rerun job."""

        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'

    class JobType(models.TextChoices):
        """Categorises the origin of a rerun request."""

        PROGRAM_RERUN = 'program_rerun', 'Program Rerun'
        NEW_ORG = 'new_org', 'New Org'
        INDIVIDUAL = 'individual', 'Individual'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Phase 2: link to the batch this job belongs to (null for Phase 1 jobs).
    batch = models.ForeignKey(
        BulkRerunBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='jobs',
    )
    # Display order within the batch.
    position = models.PositiveIntegerField(default=0)

    # Kept for backward compatibility with Phase 1 individually submitted jobs.
    bulk_job_id = models.UUIDField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='bulk_rerun_jobs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    job_type = models.CharField(
        max_length=16,
        choices=JobType.choices,
        default=JobType.INDIVIDUAL,
    )
    source_course_key = models.CharField(max_length=255)
    target_course_key = models.CharField(max_length=255)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        """Default ordering and composite indexes for common query patterns."""

        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['bulk_job_id', 'status']),
            models.Index(fields=['target_course_key']),
        ]

    settings_applied = models.BooleanField(default=False)

    @property
    def is_terminal(self):
        """Return True once the job has reached a final state and must not be restarted."""
        return self.status in (self.Status.SUCCEEDED, self.Status.FAILED)


class CourseRerunSettings(models.Model):
    """
    Operator-configured settings applied to every course in a batch after the rerun succeeds.

    Covers scheduling dates, course mode, certificate behaviour, lesson gating, and
    provisioner cleanup.  One row per BulkRerunBatch; accessed as batch.settings.

    .. no_pii: Contains course configuration metadata only; no personal data.
    """

    class Pacing(models.TextChoices):
        """Pacing modes supported by the platform."""

        INSTRUCTOR = 'instructor', 'Instructor-paced'
        SELF = 'self', 'Self-paced'

    class CourseMode(models.TextChoices):
        """Enrolment modes relevant to bulk rerun provisioning."""

        HONOR = 'honor', 'Honor'
        AUDIT = 'audit', 'Audit'
        VERIFIED = 'verified', 'Verified'

    class CertDisplay(models.TextChoices):
        """Certificate display behaviour options."""

        EARLY_NO_INFO = 'early_no_info', 'Early (no info)'
        EARLY_WITH_INFO = 'early_with_info', 'Early (with info)'
        END = 'end', 'End of course'

    class GatingMode(models.TextChoices):
        """Lesson gating strategies supported by Phase 3."""

        DISABLED = 'disabled', 'Disabled'
        COPY = 'copy', 'Copy from source'
        CUSTOM = 'custom', 'Custom (not implemented)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.OneToOneField(
        BulkRerunBatch,
        on_delete=models.CASCADE,
        related_name='settings',
    )

    # Scheduling
    course_start = models.DateTimeField()
    course_end = models.DateTimeField()
    enrollment_start = models.DateTimeField()
    enrollment_end = models.DateTimeField()
    pacing = models.CharField(max_length=16, choices=Pacing.choices, default=Pacing.INSTRUCTOR)

    # Certificates
    course_mode = models.CharField(max_length=16, choices=CourseMode.choices, default=CourseMode.HONOR)
    cert_display = models.CharField(max_length=32, choices=CertDisplay.choices, default=CertDisplay.EARLY_NO_INFO)
    create_cert = models.BooleanField(default=True)
    student_gen_cert = models.BooleanField(default=True)
    cert_on_dashboard = models.BooleanField(default=True)

    # Lesson gating
    gating_mode = models.CharField(max_length=16, choices=GatingMode.choices, default=GatingMode.DISABLED)
    gating_min_score = models.CharField(max_length=3, blank=True, default='80')
    gating_min_completion = models.CharField(max_length=3, blank=True, default='100')

    # Provisioner cleanup
    remove_provisioner_after = models.BooleanField(default=True)


class CourseRerunTeamMember(models.Model):
    """
    A single CAR (Course Assignment Roster) entry for a bulk rerun batch.

    One row per team member listed in the Step 2 Team & Access tab of the UI.
    All members are added to every course created within the batch.

    .. no_pii: Email is operational metadata used solely for user lookup during
        course provisioning; it is not displayed or processed beyond that lookup.
    """

    class StudioRole(models.TextChoices):
        """Studio course team roles."""

        ADMIN = 'admin', 'Admin'
        STAFF = 'staff', 'Staff'
        DATA_RESEARCHER = 'data_researcher', 'Data Researcher'

    class DiscussionRole(models.TextChoices):
        """Discussion forum roles."""

        DISCUSSION_ADMIN = 'discussion_admin', 'Discussion Admin'
        MODERATOR = 'moderator', 'Moderator'
        NONE = 'none', 'None'

    batch = models.ForeignKey(
        BulkRerunBatch,
        on_delete=models.CASCADE,
        related_name='team_members',
    )
    email = models.EmailField()
    studio_role = models.CharField(max_length=32, choices=StudioRole.choices, default=StudioRole.ADMIN)
    discussion_role = models.CharField(
        max_length=32,
        choices=DiscussionRole.choices,
        default=DiscussionRole.DISCUSSION_ADMIN,
    )

    class Meta:
        """Ordering and uniqueness constraints for team member entries."""

        ordering = ['email']
        unique_together = [('batch', 'email')]


class CourseRerunLog(models.Model):
    """
    Append-only structured log line for a single CourseRerunJob.

    Written by the Celery task as it executes. Read by the polling endpoint
    so the UI can display real-time progress in the Track Progress panel.

    .. no_pii: Contains operational log messages only; no personal data.
    """

    class Level(models.TextChoices):
        """Severity levels matching the UI colour coding."""

        INFO = 'info', 'Info'   # blue  #79b8ff
        OK = 'ok', 'OK'         # green #4ec994
        WARN = 'warn', 'Warn'   # amber #f0ad4e
        ERR = 'err', 'Error'    # red   #ff7b72

    # BigAutoField default gives sequential IDs usable for ?since= polling.
    job = models.ForeignKey(
        CourseRerunJob,
        on_delete=models.CASCADE,
        related_name='logs',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(
        max_length=8,
        choices=Level.choices,
        default=Level.INFO,
    )
    message = models.TextField()

    class Meta:
        """Chronological ordering; composite index for per-job log queries."""

        ordering = ['created_at']
        indexes = [
            models.Index(fields=['job', 'created_at']),
        ]
