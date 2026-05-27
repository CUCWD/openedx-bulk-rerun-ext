"""
Database models for openedx_bulk_rerun_ext.
"""
import uuid

from django.conf import settings
from django.db import models


class CourseRerunJob(models.Model):
    """
    Tracks a single source→target course rerun operation.

    One row is created per target course key. A bulk UI submission creates
    multiple rows that share the same bulk_job_id so they can be queried
    and displayed together.

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

        # Rerun from this org's prior year run.
        PROGRAM_RERUN = 'program_rerun', 'Program Rerun'
        # First-time org: clone from a Demo template course.
        NEW_ORG = 'new_org', 'New Org'
        # Single course chosen manually by a staff member.
        INDIVIDUAL = 'individual', 'Individual'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Groups all rows created from the same bulk UI submission.
    # Null for individually submitted jobs.
    bulk_job_id = models.UUIDField(null=True, blank=True, db_index=True)

    # SET_NULL so job history survives user account deletion.
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

    # unique=True enforces that the same target can never be created twice,
    # even across separate bulk submissions or retried failed jobs.
    target_course_key = models.CharField(max_length=255, unique=True)

    # Celery result ID, stored so operators can inspect the task in Flower.
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    # Populated with str(exc) when the Celery task exhausts all retries and fails.
    # Default '' (not NULL) so templates and serializers never need a null check.
    error_message = models.TextField(blank=True, default='')

    class Meta:
        """Default ordering and composite indexes for common query patterns."""

        ordering = ['-created_at']
        indexes = [
            # Supports the common query: "all jobs in this bulk submission with status X".
            models.Index(fields=['bulk_job_id', 'status']),
            # Supports the duplicate-target check in the validate endpoint.
            models.Index(fields=['target_course_key']),
        ]

    @property
    def is_terminal(self):
        """Return True once the job has reached a final state and must not be restarted."""
        return self.status in (self.Status.SUCCEEDED, self.Status.FAILED)
