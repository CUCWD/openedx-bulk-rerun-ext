"""
Tests for the run_course_rerun and dispatch_batch_rerun Celery tasks.

edx-platform modules (rerun_course, CourseRerunState) are injected via
sys.modules so they never need to be installed in the test environment.
CourseKey parsing uses the real opaque-keys library since it IS a dependency.
"""
# pylint: disable=redefined-outer-name,unused-argument
import sys
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db.utils import IntegrityError
from django.utils import timezone

from openedx_bulk_rerun_ext.models import BulkRerunBatch, CourseRerunJob, CourseRerunLog, CourseRerunSettings
from openedx_bulk_rerun_ext.tasks import (
    _check_batch_completion,
    apply_course_settings,
    dispatch_batch_rerun,
    run_course_rerun,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

SOURCE_KEY = 'course-v1:CA+FAA-ACS-AM-IA-ACE+DEMO'
TARGET_KEY = 'course-v1:AeroTech+FAA-ACS-AM-IA-ACE+TEST_RUN'


# ── Platform module mocks ─────────────────────────────────────────────────────

@pytest.fixture
def rerun_course_mock():
    mock = MagicMock()
    mock.apply.return_value = MagicMock(result='succeeded')
    return mock


@pytest.fixture
def course_rerun_state_mock():
    return MagicMock()


@pytest.fixture(autouse=True)
def mock_platform_imports(rerun_course_mock, course_rerun_state_mock):
    """
    Pre-populate sys.modules with lightweight mocks for edx-platform packages.
    Cleaned up automatically after each test by patch.dict.
    """
    modules = {
        'cms': MagicMock(),
        'cms.djangoapps': MagicMock(),
        'cms.djangoapps.contentstore': MagicMock(),
        'cms.djangoapps.contentstore.tasks': MagicMock(rerun_course=rerun_course_mock),
        'common': MagicMock(),
        'common.djangoapps': MagicMock(),
        'common.djangoapps.course_action_state': MagicMock(),
        'common.djangoapps.course_action_state.models': MagicMock(
            CourseRerunState=course_rerun_state_mock
        ),
        'organizations': MagicMock(),
        'organizations.models': MagicMock(
            OrganizationCourse=MagicMock(
                **{'objects.filter.return_value.delete.return_value': (0, {})}
            )
        ),
    }
    with patch.dict(sys.modules, modules):
        yield


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user():
    return User.objects.create_user(username='testuser', password='pw')


@pytest.fixture
def job(user):
    return CourseRerunJob.objects.create(
        source_course_key=SOURCE_KEY,
        target_course_key=TARGET_KEY,
        created_by=user,
    )


@pytest.fixture
def batch(user):
    return BulkRerunBatch.objects.create(
        created_by=user,
        mode=BulkRerunBatch.Mode.INDIVIDUAL,
        target_run='2026_2027',
    )


@pytest.fixture
def batch_job(user, batch):
    return CourseRerunJob.objects.create(
        source_course_key=SOURCE_KEY,
        target_course_key=TARGET_KEY,
        created_by=user,
        batch=batch,
        position=0,
    )


@pytest.fixture
def settings_obj(batch):
    """CourseRerunSettings row for batch; triggers the applicator path in apply_course_settings."""
    now = timezone.now()
    return CourseRerunSettings.objects.create(
        batch=batch,
        course_start=now,
        course_end=now.replace(year=now.year + 1),
        enrollment_start=now,
        enrollment_end=now.replace(year=now.year + 1),
        remove_provisioner_after=False,
    )


# ── Phase 1: guard-rail tests ─────────────────────────────────────────────────

class TestEarlyReturns:
    """Task aborts silently for non-existent or already-terminal jobs."""

    def test_nonexistent_job_id_does_not_raise(self):
        run_course_rerun.apply(args=['00000000-0000-0000-0000-000000000000'])

    def test_succeeded_job_is_skipped(self, job):
        job.status = CourseRerunJob.Status.SUCCEEDED
        job.save()
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.SUCCEEDED  # unchanged

    def test_failed_job_is_skipped(self, job):
        job.status = CourseRerunJob.Status.FAILED
        job.save()
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.FAILED  # unchanged


# ── Phase 1: success path ─────────────────────────────────────────────────────

class TestSuccessPath:
    """Job transitions to SUCCEEDED and all fields are written correctly."""

    def test_job_marked_succeeded(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.SUCCEEDED

    def test_completed_at_set_on_success(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.completed_at is not None

    def test_started_at_set_on_first_attempt(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.started_at is not None

    def test_started_at_not_overwritten_on_retry(self, job, rerun_course_mock):
        t0 = timezone.now()
        job.started_at = t0
        job.status = CourseRerunJob.Status.RUNNING
        job.save()
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.started_at == t0

    def test_rerun_course_called_with_correct_args(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        rerun_course_mock.apply.assert_called_once_with(
            args=[SOURCE_KEY, TARGET_KEY, job.created_by_id],
            kwargs={'fields': None},
        )

    def test_course_rerun_state_initiated(self, job, course_rerun_state_mock):
        run_course_rerun.apply(args=[str(job.id)])
        course_rerun_state_mock.objects.initiated.assert_called_once()

    def test_error_message_empty_on_success(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.error_message == ''


# ── Phase 1: failure path ─────────────────────────────────────────────────────

class TestFailurePath:
    """Job transitions to FAILED after all retries are exhausted."""

    def test_job_marked_failed_after_all_retries(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.FAILED

    def test_error_message_stored_on_failure(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert 'duplicate course' in job.error_message

    def test_completed_at_set_on_failure(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.completed_at is not None

    def test_rerun_course_retried_up_to_max(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='exception: something')
        run_course_rerun.apply(args=[str(job.id)])
        assert rerun_course_mock.apply.call_count == 4

    def test_exception_from_rerun_course_apply_triggers_failure(self, job, rerun_course_mock):
        rerun_course_mock.apply.side_effect = RuntimeError('boom')
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.FAILED
        assert 'boom' in job.error_message


# ── Phase 2: log output ───────────────────────────────────────────────────────

class TestRunCourseRerunLogging:
    """run_course_rerun writes structured log lines to CourseRerunLog."""

    def test_log_lines_written_on_success(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        assert job.logs.count() > 0

    def test_first_log_is_provisioning_created(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        first = job.logs.first()
        assert 'ProvisioningJob' in first.message and 'created for batch' in first.message

    def test_success_log_written_on_success(self, job, rerun_course_mock):
        run_course_rerun.apply(args=[str(job.id)])
        ok_logs = job.logs.filter(level=CourseRerunLog.Level.OK)
        assert ok_logs.exists()

    def test_error_log_written_on_failure(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(job.id)])
        err_logs = job.logs.filter(level=CourseRerunLog.Level.ERR)
        assert err_logs.exists()

    def test_error_log_contains_failure_reason(self, job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(job.id)])
        err_log = job.logs.filter(level=CourseRerunLog.Level.ERR).first()
        assert 'duplicate course' in err_log.message


# ── Phase 2: edge paths in run_course_rerun ───────────────────────────────────

class TestRunCourseRerunEdgePaths:
    """run_course_rerun handles stale org rows, template prefixes, and error recovery."""

    def test_stale_org_rows_deleted_logs_count(self, job, rerun_course_mock):
        """Deleting stale OrganizationCourse rows is logged when deleted_count > 0."""
        sys.modules['organizations.models'].OrganizationCourse.objects.filter.return_value.delete.return_value = (2, {})
        run_course_rerun.apply(args=[str(job.id)])
        assert job.logs.filter(level='info', message__icontains='Removed 2 stale').exists()

    def test_template_prefix_stripped_from_display_name(self, job, rerun_course_mock):
        """When the source course name starts with a template prefix, it is stripped."""
        src_course = MagicMock()
        src_course.display_name = 'Demo: My Course'
        mock_store = MagicMock()
        mock_store.get_course.return_value = src_course
        fake_xmodule = {
            'xmodule': MagicMock(), 'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': MagicMock(modulestore=MagicMock(return_value=mock_store)),
        }
        with patch.dict(sys.modules, fake_xmodule):
            run_course_rerun.apply(args=[str(job.id)])
        assert job.logs.filter(message__icontains='Stripped template prefix').exists()

    def test_rerun_failure_with_existing_course_dispatches_settings(self, job, rerun_course_mock):
        """When rerun_course fails but the course already exists, settings are applied instead."""
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        mock_store = MagicMock()
        mock_store.has_course.return_value = True
        fake_xmodule = {
            'xmodule': MagicMock(), 'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': MagicMock(modulestore=MagicMock(return_value=mock_store)),
        }
        with patch.dict(sys.modules, fake_xmodule), \
             patch('openedx_bulk_rerun_ext.tasks.apply_course_settings.apply'):
            run_course_rerun.apply(args=[str(job.id)])
        assert job.logs.filter(level='warn', message__icontains='already exists').exists()

    def test_integrity_error_with_target_key_dispatches_settings(self, job, rerun_course_mock):
        """An IntegrityError for the target course key triggers settings dispatch, not failure."""
        rerun_course_mock.apply.side_effect = IntegrityError(
            f'Duplicate entry for {job.target_course_key}'
        )
        with patch('openedx_bulk_rerun_ext.tasks.apply_course_settings.apply'):
            run_course_rerun.apply(args=[str(job.id)])
        assert job.logs.filter(level='warn', message__icontains='already exists').exists()

    def test_src_course_none_skips_display_name_strip(self, job, rerun_course_mock):
        """When modulestore returns None for the source course, display_name stripping is skipped."""
        mock_store = MagicMock()
        mock_store.get_course.return_value = None
        fake_xmodule = {
            'xmodule': MagicMock(), 'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': MagicMock(modulestore=MagicMock(return_value=mock_store)),
        }
        with patch.dict(sys.modules, fake_xmodule):
            run_course_rerun.apply(args=[str(job.id)])
        assert not job.logs.filter(message__icontains='Stripped template prefix').exists()

    def test_no_template_prefix_leaves_fields_json_none(self, job, rerun_course_mock):
        """A display_name without a template prefix passes the name unchanged."""
        src_course = MagicMock()
        src_course.display_name = 'Regular Course Title'
        mock_store = MagicMock()
        mock_store.get_course.return_value = src_course
        fake_xmodule = {
            'xmodule': MagicMock(), 'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': MagicMock(modulestore=MagicMock(return_value=mock_store)),
        }
        with patch.dict(sys.modules, fake_xmodule):
            run_course_rerun.apply(args=[str(job.id)])
        assert not job.logs.filter(message__icontains='Stripped template prefix').exists()


# ── Phase 2: dry-run mode ─────────────────────────────────────────────────────

class TestRunCourseRerunDryRun:
    """Dry-run jobs write logs but never call rerun_course.apply()."""

    def test_dry_run_does_not_call_rerun_course(self, batch_job, rerun_course_mock):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        run_course_rerun.apply(args=[str(batch_job.id)])
        rerun_course_mock.apply.assert_not_called()

    def test_dry_run_marks_job_succeeded(self, batch_job, rerun_course_mock):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        run_course_rerun.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.status == CourseRerunJob.Status.SUCCEEDED

    def test_dry_run_logs_contain_dry_run_prefix(self, batch_job, rerun_course_mock):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        run_course_rerun.apply(args=[str(batch_job.id)])
        messages = list(batch_job.logs.values_list('message', flat=True))
        assert any('[DRY-RUN]' in m for m in messages)

    def test_dry_run_completion_log_written(self, batch_job, rerun_course_mock):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        run_course_rerun.apply(args=[str(batch_job.id)])
        ok_logs = batch_job.logs.filter(level=CourseRerunLog.Level.OK)
        assert any('Dry-run complete' in m for m in ok_logs.values_list('message', flat=True))


# ── Phase 2: batch completion rollup ─────────────────────────────────────────

class TestCheckBatchCompletion:
    """_check_batch_completion rolls up batch status correctly."""

    def test_batch_succeeded_when_all_jobs_succeeded(self, user, batch):
        for i in range(2):
            j = CourseRerunJob.objects.create(
                source_course_key=SOURCE_KEY,
                target_course_key=f'course-v1:ORG+TEST+{i}',
                created_by=user, batch=batch,
            )
            j.status = CourseRerunJob.Status.SUCCEEDED
            j.save()
        _check_batch_completion(str(batch.id))
        batch.refresh_from_db()
        assert batch.status == BulkRerunBatch.Status.SUCCEEDED
        assert batch.completed_at is not None

    def test_batch_failed_when_all_jobs_failed(self, user, batch):
        for i in range(2):
            j = CourseRerunJob.objects.create(
                source_course_key=SOURCE_KEY,
                target_course_key=f'course-v1:ORG+TEST+{i}',
                created_by=user, batch=batch,
            )
            j.status = CourseRerunJob.Status.FAILED
            j.save()
        _check_batch_completion(str(batch.id))
        batch.refresh_from_db()
        assert batch.status == BulkRerunBatch.Status.FAILED

    def test_batch_partial_when_some_jobs_failed(self, user, batch):
        for status, key in [(CourseRerunJob.Status.SUCCEEDED, '0'), (CourseRerunJob.Status.FAILED, '1')]:
            j = CourseRerunJob.objects.create(
                source_course_key=SOURCE_KEY,
                target_course_key=f'course-v1:ORG+TEST+{key}',
                created_by=user, batch=batch,
            )
            j.status = status
            j.save()
        _check_batch_completion(str(batch.id))
        batch.refresh_from_db()
        assert batch.status == BulkRerunBatch.Status.PARTIAL

    def test_batch_not_updated_while_jobs_still_running(self, user, batch):
        j = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key='course-v1:ORG+TEST+0',
            created_by=user, batch=batch,
        )
        j.status = CourseRerunJob.Status.RUNNING
        j.save()
        _check_batch_completion(str(batch.id))
        batch.refresh_from_db()
        assert batch.status == BulkRerunBatch.Status.PENDING  # unchanged

    def test_nonexistent_batch_does_not_raise(self):
        _check_batch_completion('00000000-0000-0000-0000-000000000000')


class TestRunCourseRerunBatchCompletion:
    """run_course_rerun triggers batch rollup after the job reaches a terminal state."""

    def test_batch_completed_after_last_job_succeeds(self, batch_job, rerun_course_mock):
        run_course_rerun.apply(args=[str(batch_job.id)])
        batch_job.batch.refresh_from_db()
        assert batch_job.batch.status == BulkRerunBatch.Status.SUCCEEDED

    def test_batch_completed_after_last_job_fails(self, batch_job, rerun_course_mock):
        rerun_course_mock.apply.return_value = MagicMock(result='duplicate course')
        run_course_rerun.apply(args=[str(batch_job.id)])
        batch_job.batch.refresh_from_db()
        assert batch_job.batch.status == BulkRerunBatch.Status.FAILED


# ── Phase 2: dispatch_batch_rerun ─────────────────────────────────────────────

class TestDispatchBatchRerun:
    """dispatch_batch_rerun marks the batch running and schedules child tasks."""

    def test_batch_marked_running(self, user, batch):
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=user, batch=batch, position=0,
        )
        with patch('openedx_bulk_rerun_ext.tasks.run_course_rerun.apply'):
            dispatch_batch_rerun.apply(args=[str(batch.id)])
        batch.refresh_from_db()
        assert batch.status == BulkRerunBatch.Status.RUNNING

    def test_child_task_dispatched_per_job(self, user, batch):
        for i in range(3):
            CourseRerunJob.objects.create(
                source_course_key=SOURCE_KEY,
                target_course_key=f'course-v1:ORG+TEST+{i}',
                created_by=user, batch=batch, position=i,
            )
        with patch('openedx_bulk_rerun_ext.tasks.run_course_rerun.apply') as mock_apply:
            dispatch_batch_rerun.apply(args=[str(batch.id)])
        assert mock_apply.call_count == 3

    def test_countdown_staggered_by_position(self, user, batch, settings):
        settings.BULK_RERUN_USE_CELERY = True
        for i in range(2):
            CourseRerunJob.objects.create(
                source_course_key=SOURCE_KEY,
                target_course_key=f'course-v1:ORG+TEST+{i}',
                created_by=user, batch=batch, position=i,
            )
        with patch('openedx_bulk_rerun_ext.tasks.run_course_rerun.apply_async') as mock_async:
            dispatch_batch_rerun.apply(args=[str(batch.id)])
        countdowns = [call.kwargs['countdown'] for call in mock_async.call_args_list]
        assert countdowns == [0, 2]

    def test_nonexistent_batch_does_not_raise(self):
        dispatch_batch_rerun.apply(args=['00000000-0000-0000-0000-000000000000'])

    def test_only_pending_jobs_are_dispatched(self, user, batch):
        running_job = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key='course-v1:ORG+TEST+0',
            created_by=user, batch=batch, position=0,
        )
        running_job.status = CourseRerunJob.Status.RUNNING
        running_job.save()
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key='course-v1:ORG+TEST+1',
            created_by=user, batch=batch, position=1,
        )
        with patch('openedx_bulk_rerun_ext.tasks.run_course_rerun.apply') as mock_apply:
            dispatch_batch_rerun.apply(args=[str(batch.id)])
        assert mock_apply.call_count == 1


# ── Phase 3: apply_course_settings — no settings row ─────────────────────────

class TestApplyCourseSettingsNoSettings:
    """apply_course_settings finalizes immediately when the batch has no settings row."""

    def test_job_marked_succeeded_for_standalone_job(self, job):
        job.status = CourseRerunJob.Status.RUNNING
        job.save()
        apply_course_settings.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.SUCCEEDED

    def test_job_marked_succeeded_for_batch_without_settings(self, batch_job):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.status == CourseRerunJob.Status.SUCCEEDED

    def test_completion_log_written(self, batch_job):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        ok_logs = batch_job.logs.filter(level=CourseRerunLog.Level.OK)
        assert ok_logs.filter(message__icontains='Course creation complete').exists()

    def test_dry_run_log_written_when_no_settings(self, batch_job):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        ok_logs = list(batch_job.logs.filter(level=CourseRerunLog.Level.OK).values_list('message', flat=True))
        assert any('Dry-run complete' in m for m in ok_logs)

    def test_nonexistent_job_does_not_raise(self):
        apply_course_settings.apply(args=['00000000-0000-0000-0000-000000000000'])

    def test_terminal_job_is_skipped(self, job):
        job.status = CourseRerunJob.Status.SUCCEEDED
        job.save()
        apply_course_settings.apply(args=[str(job.id)])
        assert job.logs.count() == 0


# ── Phase 3: apply_course_settings — with settings row ───────────────────────

class TestApplyCourseSettingsWithSettings:
    """apply_course_settings runs applicators and sets settings_applied when a settings row exists."""

    def test_job_marked_succeeded(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.status == CourseRerunJob.Status.SUCCEEDED

    def test_settings_applied_flag_set(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.settings_applied is True

    def test_completion_log_written(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        ok_logs = batch_job.logs.filter(level=CourseRerunLog.Level.OK)
        assert ok_logs.filter(message__icontains='Course creation complete').exists()

    def test_batch_completion_triggered(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.batch.refresh_from_db()
        assert batch_job.batch.status == BulkRerunBatch.Status.SUCCEEDED

    def test_apply_gating_called_when_mode_is_not_disabled(self, batch_job, user):
        """apply_gating is invoked when gating_mode is not 'disabled'."""
        now = timezone.now()
        CourseRerunSettings.objects.create(
            batch=batch_job.batch,
            course_start=now,
            course_end=now.replace(year=now.year + 1),
            enrollment_start=now,
            enrollment_end=now.replace(year=now.year + 1),
            gating_mode='copy',
            remove_provisioner_after=False,
        )
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        assert batch_job.logs.filter(message__icontains='gating').exists()

    def test_remove_provisioner_called_when_flag_true(self, batch_job, user):
        """remove_provisioner is invoked when remove_provisioner_after is True."""
        now = timezone.now()
        CourseRerunSettings.objects.create(
            batch=batch_job.batch,
            course_start=now,
            course_end=now.replace(year=now.year + 1),
            enrollment_start=now,
            enrollment_end=now.replace(year=now.year + 1),
            remove_provisioner_after=True,
        )
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        assert batch_job.logs.filter(message__icontains='Provisioner').exists()


# ── Phase 3: apply_course_settings — dry-run with settings ───────────────────

class TestApplyCourseSettingsDryRun:
    """apply_course_settings writes [DRY-RUN] logs but skips platform calls."""

    def test_dry_run_logs_contain_dry_run_prefix(self, batch_job, settings_obj):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        messages = list(batch_job.logs.values_list('message', flat=True))
        assert any('[DRY-RUN]' in m for m in messages)

    def test_dry_run_marks_job_succeeded(self, batch_job, settings_obj):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.status == CourseRerunJob.Status.SUCCEEDED

    def test_dry_run_sets_settings_applied(self, batch_job, settings_obj):
        batch_job.batch.is_dry_run = True
        batch_job.batch.save()
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.settings_applied is True


# ── Phase 3: apply_course_settings — failure ─────────────────────────────────

class TestApplyCourseSettingsFailure:
    """apply_course_settings marks job failed after max retries are exhausted."""

    def test_job_marked_failed_on_unrecoverable_error(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        with patch(
            'openedx_bulk_rerun_ext.applicators.apply_scheduling',
            side_effect=RuntimeError('scheduling boom'),
        ):
            apply_course_settings.apply(args=[str(batch_job.id)])
        batch_job.refresh_from_db()
        assert batch_job.status == CourseRerunJob.Status.FAILED
        assert 'scheduling boom' in batch_job.error_message

    def test_error_log_written_on_failure(self, batch_job, settings_obj):
        batch_job.status = CourseRerunJob.Status.RUNNING
        batch_job.save()
        with patch(
            'openedx_bulk_rerun_ext.applicators.apply_scheduling',
            side_effect=RuntimeError('scheduling boom'),
        ):
            apply_course_settings.apply(args=[str(batch_job.id)])
        assert batch_job.logs.filter(level=CourseRerunLog.Level.ERR).exists()
