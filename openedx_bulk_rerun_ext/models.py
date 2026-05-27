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
    target_course_key = models.CharField(max_length=255, unique=True)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        """Default ordering and composite indexes for common query patterns."""

        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['bulk_job_id', 'status']),
            models.Index(fields=['target_course_key']),
        ]

    @property
    def is_terminal(self):
        """Return True once the job has reached a final state and must not be restarted."""
        return self.status in (self.Status.SUCCEEDED, self.Status.FAILED)


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
