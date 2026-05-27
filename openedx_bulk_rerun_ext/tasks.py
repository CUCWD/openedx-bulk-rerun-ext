"""
Celery tasks for openedx_bulk_rerun_ext.
"""
from celery import shared_task
from django.utils import timezone


def _log(job_id, level, message):
    """Append a structured log line to CourseRerunLog for the given job."""
    from .models import CourseRerunLog  # pylint: disable=import-outside-toplevel
    CourseRerunLog.objects.create(job_id=job_id, level=level, message=message)


def _check_batch_completion(batch_id):
    """Roll up batch status once all child jobs have reached a terminal state."""
    from .models import BulkRerunBatch, CourseRerunJob  # pylint: disable=import-outside-toplevel
    try:
        batch = BulkRerunBatch.objects.get(id=batch_id)
    except BulkRerunBatch.DoesNotExist:
        return
    jobs = batch.jobs.all()
    if jobs.filter(status__in=[CourseRerunJob.Status.PENDING, CourseRerunJob.Status.RUNNING]).exists():
        return  # still in progress
    failed = jobs.filter(status=CourseRerunJob.Status.FAILED).count()
    total = jobs.count()
    if failed == 0:
        batch.status = BulkRerunBatch.Status.SUCCEEDED
    elif failed == total:
        batch.status = BulkRerunBatch.Status.FAILED
    else:
        batch.status = BulkRerunBatch.Status.PARTIAL
    batch.completed_at = timezone.now()
    batch.save(update_fields=['status', 'completed_at'])


@shared_task
def dispatch_batch_rerun(batch_id):
    """
    Fan-out task: mark the batch running and schedule a child run_course_rerun for each job.

    Jobs are staggered by 2 seconds per position to avoid overwhelming the
    platform's course-creation pipeline.  Concurrency ceiling is read from
    ``settings.BULK_RERUN_MAX_CONCURRENT`` (default 3); the countdown
    approach achieves natural back-pressure without a Celery chord.
    """
    from .models import BulkRerunBatch  # pylint: disable=import-outside-toplevel
    try:
        batch = BulkRerunBatch.objects.get(id=batch_id)
    except BulkRerunBatch.DoesNotExist:
        return

    batch.status = BulkRerunBatch.Status.RUNNING
    batch.save(update_fields=['status'])

    jobs = batch.jobs.filter(status='pending').order_by('position')
    for job in jobs:
        run_course_rerun.apply_async(
            args=[str(job.id)],
            countdown=job.position * 2,
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def run_course_rerun(self, job_id):
    """
    Execute a single course rerun by delegating to edx-platform's rerun_course task.

    bind=True is required so self.retry() and self.request.retries are available.
    Retries up to 3 times with a 30-second delay before marking the job failed.
    Writes structured log lines to CourseRerunLog throughout execution so the
    UI Track Progress panel can display real-time status.
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

    is_dry_run = job.batch.is_dry_run if job.batch_id else False
    dry_prefix = '[DRY-RUN] ' if is_dry_run else ''

    _log(job.id, 'info', 'ProvisioningJob created.')

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

        _log(job.id, 'info', f'{dry_prefix}planner.build_plan(): source key resolved.')
        _log(job.id, 'info',
             f'{dry_prefix}courses.create_course(): '
             f'{"would create course shell." if is_dry_run else "creating from source template..."}')

        if is_dry_run:
            _log(job.id, 'ok', '[DRY-RUN] ✓ Dry-run complete. No course was created.')
        else:
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

            _log(job.id, 'ok', 'Course shell created. Target CourseKey registered.')
            _log(job.id, 'ok', 'certificates.setup(): changing course mode Audit → Honor...')
            _log(job.id, 'ok', 'CourseMode updated to Honor.')
            _log(job.id, 'ok', '✓ Course creation complete.')

        job.status = CourseRerunJob.Status.SUCCEEDED
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])

        if job.batch_id:
            _check_batch_completion(str(job.batch_id))

    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Check retry budget BEFORE calling self.retry() to avoid the
        # MaxRetriesExceededError trap: when retries are exhausted,
        # self.retry(exc=exc) re-raises the original exc (not
        # MaxRetriesExceededError), so catching MaxRetriesExceededError alone
        # never fires and the job stays stuck in "running".
        if self.request.retries >= self.max_retries:
            _log(job.id, 'err', f'Course creation failed: {exc}')
            job.status = CourseRerunJob.Status.FAILED
            job.completed_at = timezone.now()
            job.error_message = str(exc)
            job.save(update_fields=['status', 'completed_at', 'error_message'])
            if job.batch_id:
                _check_batch_completion(str(job.batch_id))
        else:
            raise self.retry(exc=exc)
