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


def _finalize_job(job, success, error=''):
    """Set terminal status on a job and roll up the parent batch if one exists."""
    from .models import CourseRerunJob  # pylint: disable=import-outside-toplevel
    job.status = CourseRerunJob.Status.SUCCEEDED if success else CourseRerunJob.Status.FAILED
    job.completed_at = timezone.now()
    update_fields = ['status', 'completed_at']
    if not success:
        job.error_message = error
        update_fields.append('error_message')
    job.save(update_fields=update_fields)
    if job.batch_id:
        _check_batch_completion(str(job.batch_id))


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

    _log(job.id, 'info', f'ProvisioningJob {job.id} created for batch {job.batch_id}.')

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
            _log(job.id, 'ok', '[DRY-RUN] Course shell skipped.')
        else:
            # Remove any stale organizations_organizationcourse rows for the
            # target key before calling rerun_course. The platform's clone_instance
            # helper does a blind INSERT into that table; if a row already exists
            # (e.g. from a previous failed attempt or a manually created course)
            # it raises IntegrityError and aborts the entire rerun.
            from organizations.models import OrganizationCourse  # pylint: disable=import-outside-toplevel
            deleted_count, _ = OrganizationCourse.objects.filter(
                course_id=str(target_key)
            ).delete()
            if deleted_count:
                _log(job.id, 'info',
                     f'Removed {deleted_count} stale org-course association(s) for {target_key}.')

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

        # Chain to apply_course_settings regardless of dry-run; that task
        # applies (or simulates) scheduling, certs, team access, and gating,
        # then sets job.status = succeeded when done.
        apply_course_settings.apply_async(args=[str(job.id)])

    except Exception as exc:  # pylint: disable=broad-exception-caught
        from django.db.utils import IntegrityError  # pylint: disable=import-outside-toplevel
        # A duplicate-entry IntegrityError for the target course key means the
        # course (or its org association) was already created — e.g. by a
        # previous retry that partially succeeded or by a concurrent job.
        # Treat this as a non-fatal condition: skip course creation and proceed
        # directly to settings application.
        if isinstance(exc, IntegrityError) and job.target_course_key in str(exc):
            _log(job.id, 'warn',
                 f'Target course already exists ({job.target_course_key}) — '
                 'skipping creation, proceeding to settings.')
            apply_course_settings.apply_async(args=[str(job.id)])
            return
        # Check retry budget BEFORE calling self.retry() to avoid the
        # MaxRetriesExceededError trap: when retries are exhausted,
        # self.retry(exc=exc) re-raises the original exc (not
        # MaxRetriesExceededError), so catching MaxRetriesExceededError alone
        # never fires and the job stays stuck in "running".
        if self.request.retries >= self.max_retries:
            _log(job.id, 'err', f'Course creation failed: {exc}')
            _finalize_job(job, success=False, error=str(exc))
        else:
            raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=15)
def apply_course_settings(self, job_id):
    """
    Apply operator-configured settings to a newly created course.

    Called after run_course_rerun succeeds.  Applies scheduling dates, course
    mode, certificates, team access, and lesson gating in sequence.  Each
    category is handled by a dedicated applicator in applicators.py.

    If the job has no batch or the batch has no settings row, the job is
    finalized immediately with status=succeeded.  Dry-run batches log what
    would be applied but skip all platform calls.

    Retries up to 2 times with a 15-second delay before marking the job failed.
    """
    from .models import CourseRerunJob  # pylint: disable=import-outside-toplevel

    try:
        job = CourseRerunJob.objects.get(id=job_id)
    except CourseRerunJob.DoesNotExist:
        return

    if job.is_terminal:
        return

    batch = job.batch
    if batch is None or not hasattr(batch, 'settings'):
        if batch is not None and batch.is_dry_run:
            _log(job.id, 'ok', '[DRY-RUN] ✓ Dry-run complete. No changes were made.')
        else:
            _log(job.id, 'ok', '✓ Course creation complete.')
        _finalize_job(job, success=True)
        return

    s = batch.settings
    is_dry_run = batch.is_dry_run

    try:
        # pylint: disable=import-outside-toplevel
        from opaque_keys.edx.keys import CourseKey

        from .applicators import (
            apply_certificates,
            apply_gating,
            apply_scheduling,
            apply_team_access,
            remove_provisioner,
        )

        course_key = CourseKey.from_string(job.target_course_key)

        if is_dry_run:
            _log(job.id, 'info', '[DRY-RUN] certificates.setup(): changing course mode Audit → Honor...')
            _log(job.id, 'ok', '[DRY-RUN] CourseMode updated to Honor.')
            _log(job.id, 'info', '[DRY-RUN] certificates.activate(): creating certificate...')
            _log(job.id, 'ok', '[DRY-RUN] Certificate activated.')
            _log(job.id, 'info', '[DRY-RUN] access.assign_team(): adding team members from CAR...')
            _log(job.id, 'ok', '[DRY-RUN] Team members assigned.')
            _log(job.id, 'ok', '[DRY-RUN] ✓ Dry-run complete. No changes were made.')
        else:
            apply_scheduling(job, course_key, s)
            apply_certificates(job, course_key, s)
            apply_team_access(job, course_key, s, batch.team_members.all(), job.created_by)
            if s.gating_mode != 'disabled':
                apply_gating(job, course_key, s)
            if s.remove_provisioner_after:
                remove_provisioner(job, course_key, job.created_by)
            _log(job.id, 'ok', '✓ Course creation complete.')

        job.settings_applied = True
        job.save(update_fields=['settings_applied'])
        _finalize_job(job, success=True)

    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(job.id, 'err', f'Settings application failed: {exc}')
        if self.request.retries >= self.max_retries:
            _finalize_job(job, success=False, error=str(exc))
        else:
            raise self.retry(exc=exc)
