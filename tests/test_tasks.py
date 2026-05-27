"""
Tests for the run_course_rerun Celery task.

edx-platform modules (rerun_course, CourseRerunState) are injected via
sys.modules so they never need to be installed in the test environment.
CourseKey parsing uses the real opaque-keys library since it IS a dependency.
"""
# pylint: disable=redefined-outer-name,unused-argument
import sys
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openedx_bulk_rerun_ext.models import CourseRerunJob
from openedx_bulk_rerun_ext.tasks import run_course_rerun

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


# ── Guard-rail tests (no platform calls needed) ───────────────────────────────

class TestEarlyReturns:
    """Task aborts silently for non-existent or already-terminal jobs."""
    def test_nonexistent_job_id_does_not_raise(self):
        # Task should silently abort when the job row is gone (e.g. race condition).
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


# ── Success path ──────────────────────────────────────────────────────────────

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
        # Prime the job with an existing started_at so a retry can't overwrite it.
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


# ── Failure path ──────────────────────────────────────────────────────────────

class TestFailurePath:
    """Job transitions to FAILED after all retries are exhausted."""
    def test_job_marked_failed_after_all_retries(self, job, rerun_course_mock):
        # rerun_course returns a non-'succeeded' string, triggering RuntimeError.
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
        # With max_retries=3 there are 4 total attempts (1 initial + 3 retries).
        rerun_course_mock.apply.return_value = MagicMock(result='exception: something')
        run_course_rerun.apply(args=[str(job.id)])
        assert rerun_course_mock.apply.call_count == 4

    def test_exception_from_rerun_course_apply_triggers_failure(self, job, rerun_course_mock):
        # If rerun_course.apply() itself raises (rather than returning an error
        # string), the job should still end up FAILED, not RUNNING.
        rerun_course_mock.apply.side_effect = RuntimeError('boom')
        run_course_rerun.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == CourseRerunJob.Status.FAILED
        assert 'boom' in job.error_message
