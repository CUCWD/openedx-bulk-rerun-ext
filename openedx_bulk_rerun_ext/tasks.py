"""
Celery tasks for openedx_bulk_rerun_ext.
"""
from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def run_course_rerun(self, job_id):
    """
    Execute a single course rerun by delegating to edx-platform's rerun_course task.

    bind=True is required so self.retry() and self.request.retries are available.
    Retries up to 3 times with a 30-second delay before marking the job failed.
    """
    from .models import CourseRerunJob  # pylint: disable=import-outside-toplevel

    try:
        job = CourseRerunJob.objects.get(id=job_id)
    except CourseRerunJob.DoesNotExist:
        return

    # Guard against duplicate execution if the task is replayed after a
    # transient broker failure that already completed the job.
    if job.is_terminal:
        return

    job.status = CourseRerunJob.Status.RUNNING
    # Only record started_at on the first attempt; retries preserve the original time.
    update_fields = ['status']
    if job.started_at is None:
        job.started_at = timezone.now()
        update_fields.append('started_at')
    job.save(update_fields=update_fields)

    try:
        # pylint: disable=import-outside-toplevel,import-error
        from cms.djangoapps.contentstore.tasks import rerun_course
        from common.djangoapps.course_action_state.models import CourseRerunState
        from opaque_keys.edx.keys import CourseKey

        source_key = CourseKey.from_string(job.source_course_key)
        target_key = CourseKey.from_string(job.target_course_key)

        # Create the CourseRerunState row so rerun_course can update it to
        # succeeded/failed. allow_not_found=True makes this safe on retries.
        CourseRerunState.objects.initiated(
            source_course_key=source_key,
            destination_course_key=target_key,
            user=job.created_by,
            display_name='',
        )

        # .apply() runs rerun_course synchronously in the current process.
        # NEVER call result.get() inside a Celery task — Celery raises
        # RuntimeError("Never call result.get() within a task!") to prevent
        # deadlocks with the thread pool, even in TASK_ALWAYS_EAGER mode.
        # rerun_course catches all exceptions internally and returns a string;
        # "succeeded" is the only value that means the course was cloned.
        result = rerun_course.apply(
            args=[str(source_key), str(target_key), job.created_by_id],
            kwargs={'fields': None},
        )
        if result.result != 'succeeded':
            raise RuntimeError(f"rerun_course did not succeed: {result.result!r}")

        job.status = CourseRerunJob.Status.SUCCEEDED
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])

    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Check retry budget BEFORE calling self.retry() to avoid the
        # MaxRetriesExceededError trap: when retries are exhausted,
        # self.retry(exc=exc) re-raises the original exc (not
        # MaxRetriesExceededError), so catching MaxRetriesExceededError alone
        # never fires and the job stays stuck in "running".
        if self.request.retries >= self.max_retries:
            job.status = CourseRerunJob.Status.FAILED
            job.completed_at = timezone.now()
            job.error_message = str(exc)
            job.save(update_fields=['status', 'completed_at', 'error_message'])
        else:
            raise self.retry(exc=exc)
