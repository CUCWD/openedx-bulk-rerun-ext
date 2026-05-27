"""
Tests for CourseRerunJob, BulkRerunBatch, and CourseRerunLog models.
"""
# pylint: disable=redefined-outer-name
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from openedx_bulk_rerun_ext.models import BulkRerunBatch, CourseRerunJob, CourseRerunLog

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username='testuser', password='pw')


@pytest.fixture
def job(user):
    return CourseRerunJob.objects.create(
        source_course_key='course-v1:CA+FAA-ACS-AM-IA-ACE+DEMO',
        target_course_key='course-v1:AeroTech+FAA-ACS-AM-IA-ACE+2026_2027',
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
        source_course_key='course-v1:CA+FAA-ACS-AM-IA-ACE+DEMO',
        target_course_key='course-v1:AeroTech+FAA-ACS-AM-IA-ACE+2026_2027',
        created_by=user,
        batch=batch,
        position=0,
    )


# ── Phase 1: CourseRerunJob ───────────────────────────────────────────────────

class TestIsTerminal:
    """is_terminal returns True only for SUCCEEDED and FAILED."""

    def test_succeeded_is_terminal(self, job):
        job.status = CourseRerunJob.Status.SUCCEEDED
        assert job.is_terminal is True

    def test_failed_is_terminal(self, job):
        job.status = CourseRerunJob.Status.FAILED
        assert job.is_terminal is True

    def test_pending_not_terminal(self, job):
        job.status = CourseRerunJob.Status.PENDING
        assert job.is_terminal is False

    def test_running_not_terminal(self, job):
        job.status = CourseRerunJob.Status.RUNNING
        assert job.is_terminal is False


class TestDefaults:
    """Newly created jobs have the expected field defaults."""

    def test_status_defaults_to_pending(self, job):
        assert job.status == CourseRerunJob.Status.PENDING

    def test_job_type_defaults_to_individual(self, job):
        assert job.job_type == CourseRerunJob.JobType.INDIVIDUAL

    def test_error_message_defaults_to_empty_string(self, job):
        assert job.error_message == ''

    def test_id_is_uuid(self, job):
        assert isinstance(job.id, uuid.UUID)

    def test_bulk_job_id_defaults_to_none(self, job):
        assert job.bulk_job_id is None

    def test_celery_task_id_defaults_to_none(self, job):
        assert job.celery_task_id is None

    def test_started_at_and_completed_at_default_to_none(self, job):
        assert job.started_at is None
        assert job.completed_at is None

    def test_created_at_is_set_automatically(self, job):
        assert job.created_at is not None

    def test_batch_defaults_to_none(self, job):
        assert job.batch is None

    def test_position_defaults_to_zero(self, job):
        assert job.position == 0


class TestConstraints:
    """DB-level constraints and meta ordering behave as specified."""

    def test_target_course_key_must_be_unique(self, user):
        CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+2026',
            created_by=user,
        )
        with pytest.raises(IntegrityError):
            CourseRerunJob.objects.create(
                source_course_key='course-v1:CA+TEST+DEMO',
                target_course_key='course-v1:ORG+TEST+2026',
                created_by=user,
            )

    def test_default_ordering_is_newest_first(self, user):
        j1 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+2025',
            created_by=user,
        )
        j2 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+2026',
            created_by=user,
        )
        ids = list(CourseRerunJob.objects.values_list('id', flat=True))
        assert ids[0] == j2.id
        assert ids[1] == j1.id

    def test_created_by_set_null_on_user_delete(self, user, job):
        user.delete()
        job.refresh_from_db()
        assert job.created_by is None

    def test_bulk_job_id_groups_related_jobs(self, user):
        group_id = uuid.uuid4()
        j1 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+A+2026',
            bulk_job_id=group_id,
            created_by=user,
        )
        j2 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+B+2026',
            bulk_job_id=group_id,
            created_by=user,
        )
        grouped = CourseRerunJob.objects.filter(bulk_job_id=group_id)
        assert set(grouped.values_list('id', flat=True)) == {j1.id, j2.id}


# ── Phase 2: BulkRerunBatch ───────────────────────────────────────────────────

class TestBulkRerunBatchDefaults:
    """Newly created batches have the expected field defaults."""

    def test_status_defaults_to_pending(self, batch):
        assert batch.status == BulkRerunBatch.Status.PENDING

    def test_id_is_uuid(self, batch):
        assert isinstance(batch.id, uuid.UUID)

    def test_is_dry_run_defaults_to_false(self, batch):
        assert batch.is_dry_run is False

    def test_prog_id_defaults_to_empty_string(self, batch):
        assert batch.prog_id == ''

    def test_completed_at_defaults_to_none(self, batch):
        assert batch.completed_at is None

    def test_created_at_set_automatically(self, batch):
        assert batch.created_at is not None


class TestBulkRerunBatchIsTerminal:
    """is_terminal returns True only for SUCCEEDED, FAILED, and PARTIAL."""

    def test_succeeded_is_terminal(self, batch):
        batch.status = BulkRerunBatch.Status.SUCCEEDED
        assert batch.is_terminal is True

    def test_failed_is_terminal(self, batch):
        batch.status = BulkRerunBatch.Status.FAILED
        assert batch.is_terminal is True

    def test_partial_is_terminal(self, batch):
        batch.status = BulkRerunBatch.Status.PARTIAL
        assert batch.is_terminal is True

    def test_pending_not_terminal(self, batch):
        batch.status = BulkRerunBatch.Status.PENDING
        assert batch.is_terminal is False

    def test_running_not_terminal(self, batch):
        batch.status = BulkRerunBatch.Status.RUNNING
        assert batch.is_terminal is False


class TestBulkRerunBatchCounts:
    """total_jobs, done_jobs, and failed_jobs reflect the linked job states."""

    def test_empty_batch_has_zero_counts(self, batch):
        assert batch.total_jobs == 0
        assert batch.done_jobs == 0
        assert batch.failed_jobs == 0

    def test_total_jobs_counts_all_jobs(self, user, batch):
        for i in range(3):
            CourseRerunJob.objects.create(
                source_course_key='course-v1:CA+TEST+DEMO',
                target_course_key=f'course-v1:ORG+TEST+RUN{i}',
                created_by=user,
                batch=batch,
            )
        assert batch.total_jobs == 3

    def test_done_jobs_counts_succeeded_and_failed(self, user, batch):
        j1 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+A',
            created_by=user, batch=batch,
        )
        j2 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+B',
            created_by=user, batch=batch,
        )
        CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+C',
            created_by=user, batch=batch,
        )
        j1.status = CourseRerunJob.Status.SUCCEEDED
        j1.save()
        j2.status = CourseRerunJob.Status.FAILED
        j2.save()
        assert batch.done_jobs == 2

    def test_failed_jobs_counts_only_failed(self, user, batch):
        j1 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+A',
            created_by=user, batch=batch,
        )
        j2 = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+B',
            created_by=user, batch=batch,
        )
        j1.status = CourseRerunJob.Status.SUCCEEDED
        j1.save()
        j2.status = CourseRerunJob.Status.FAILED
        j2.save()
        assert batch.failed_jobs == 1

    def test_pending_jobs_not_counted_as_done(self, user, batch):
        CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+A',
            created_by=user, batch=batch,
        )
        assert batch.done_jobs == 0


# ── Phase 2: CourseRerunLog ───────────────────────────────────────────────────

class TestCourseRerunLog:
    """CourseRerunLog stores append-only structured log lines per job."""

    def test_log_created_with_info_default(self, batch_job):
        log = CourseRerunLog.objects.create(job=batch_job, message='started')
        assert log.level == CourseRerunLog.Level.INFO
        assert log.created_at is not None

    def test_all_levels_accepted(self, batch_job):
        for level in ('info', 'ok', 'warn', 'err'):
            CourseRerunLog.objects.create(job=batch_job, level=level, message=f'{level} msg')
        assert batch_job.logs.count() == 4

    def test_logs_ordered_chronologically(self, batch_job):
        CourseRerunLog.objects.create(job=batch_job, message='first')
        CourseRerunLog.objects.create(job=batch_job, message='second')
        messages = list(batch_job.logs.values_list('message', flat=True))
        assert messages == ['first', 'second']

    def test_logs_cascade_deleted_with_job(self, user, batch):
        job = CourseRerunJob.objects.create(
            source_course_key='course-v1:CA+TEST+DEMO',
            target_course_key='course-v1:ORG+TEST+DEL',
            created_by=user, batch=batch,
        )
        CourseRerunLog.objects.create(job=job, message='will be deleted')
        job_id = job.id
        job.delete()
        assert CourseRerunLog.objects.filter(job_id=job_id).count() == 0

    def test_since_query_pattern(self, batch_job):
        l1 = CourseRerunLog.objects.create(job=batch_job, message='line 1')
        CourseRerunLog.objects.create(job=batch_job, message='line 2')
        CourseRerunLog.objects.create(job=batch_job, message='line 3')
        newer = list(batch_job.logs.filter(id__gt=l1.id).values_list('message', flat=True))
        assert newer == ['line 2', 'line 3']
