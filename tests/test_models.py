"""
Tests for CourseRerunJob, BulkRerunBatch, CourseRerunLog, CourseRerunSettings,
and CourseRerunTeamMember models.
"""
# pylint: disable=redefined-outer-name
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from openedx_bulk_rerun_ext.models import (
    BulkRerunBatch,
    CourseRerunJob,
    CourseRerunLog,
    CourseRerunSettings,
    CourseRerunTeamMember,
)

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


# ── Phase 3: CourseRerunJob.settings_applied ──────────────────────────────────

class TestCourseRerunJobSettingsApplied:
    """settings_applied defaults to False and can be flipped to True."""

    def test_settings_applied_defaults_to_false(self, job):
        assert job.settings_applied is False

    def test_settings_applied_can_be_set(self, job):
        job.settings_applied = True
        job.save(update_fields=['settings_applied'])
        job.refresh_from_db()
        assert job.settings_applied is True


# ── Phase 3: CourseRerunSettings ──────────────────────────────────────────────

def _make_settings(batch):
    """Create a CourseRerunSettings row with valid scheduling dates for the given batch."""
    now = timezone.now()
    return CourseRerunSettings.objects.create(
        batch=batch,
        course_start=now,
        course_end=now.replace(year=now.year + 1),
        enrollment_start=now,
        enrollment_end=now.replace(year=now.year + 1),
    )


class TestCourseRerunSettingsDefaults:
    """Newly created settings rows have the expected field defaults."""

    def test_pacing_defaults_to_instructor(self, batch):
        s = _make_settings(batch)
        assert s.pacing == CourseRerunSettings.Pacing.INSTRUCTOR

    def test_course_mode_defaults_to_honor(self, batch):
        s = _make_settings(batch)
        assert s.course_mode == CourseRerunSettings.CourseMode.HONOR

    def test_cert_display_defaults_to_early_no_info(self, batch):
        s = _make_settings(batch)
        assert s.cert_display == CourseRerunSettings.CertDisplay.EARLY_NO_INFO

    def test_create_cert_defaults_to_true(self, batch):
        s = _make_settings(batch)
        assert s.create_cert is True

    def test_student_gen_cert_defaults_to_true(self, batch):
        s = _make_settings(batch)
        assert s.student_gen_cert is True

    def test_cert_on_dashboard_defaults_to_true(self, batch):
        s = _make_settings(batch)
        assert s.cert_on_dashboard is True

    def test_gating_mode_defaults_to_disabled(self, batch):
        s = _make_settings(batch)
        assert s.gating_mode == CourseRerunSettings.GatingMode.DISABLED

    def test_gating_template_id_defaults_to_empty(self, batch):
        s = _make_settings(batch)
        assert s.gating_template_id == ''

    def test_remove_provisioner_after_defaults_to_true(self, batch):
        s = _make_settings(batch)
        assert s.remove_provisioner_after is True

    def test_id_is_uuid(self, batch):
        s = _make_settings(batch)
        assert isinstance(s.id, uuid.UUID)


class TestCourseRerunSettingsRelationship:
    """CourseRerunSettings is accessible via the OneToOne reverse accessor."""

    def test_batch_settings_accessor_returns_row(self, batch):
        s = _make_settings(batch)
        assert batch.settings.id == s.id

    def test_settings_cascade_deleted_with_batch(self, batch):
        s = _make_settings(batch)
        settings_id = s.id
        batch.delete()
        assert not CourseRerunSettings.objects.filter(id=settings_id).exists()

    def test_second_settings_row_raises_integrity_error(self, batch):
        _make_settings(batch)
        with pytest.raises(Exception):
            _make_settings(batch)


# ── Phase 3: CourseRerunTeamMember ────────────────────────────────────────────

class TestCourseRerunTeamMemberDefaults:
    """Newly created team member rows have the expected field defaults."""

    def test_studio_role_defaults_to_admin(self, batch):
        m = CourseRerunTeamMember.objects.create(batch=batch, email='a@example.com')
        assert m.studio_role == CourseRerunTeamMember.StudioRole.ADMIN

    def test_discussion_role_defaults_to_discussion_admin(self, batch):
        m = CourseRerunTeamMember.objects.create(batch=batch, email='a@example.com')
        assert m.discussion_role == CourseRerunTeamMember.DiscussionRole.DISCUSSION_ADMIN


class TestCourseRerunTeamMemberConstraints:
    """unique_together and ordering are enforced at the DB level."""

    def test_unique_together_prevents_duplicate_email_in_same_batch(self, batch):
        CourseRerunTeamMember.objects.create(batch=batch, email='dup@example.com')
        with pytest.raises(IntegrityError):
            CourseRerunTeamMember.objects.create(batch=batch, email='dup@example.com')

    def test_same_email_allowed_in_different_batches(self, user, batch):
        second_batch = BulkRerunBatch.objects.create(
            created_by=user, mode=BulkRerunBatch.Mode.INDIVIDUAL, target_run='2027_2028'
        )
        CourseRerunTeamMember.objects.create(batch=batch, email='shared@example.com')
        CourseRerunTeamMember.objects.create(batch=second_batch, email='shared@example.com')
        assert CourseRerunTeamMember.objects.filter(email='shared@example.com').count() == 2

    def test_ordering_is_by_email(self, batch):
        CourseRerunTeamMember.objects.create(batch=batch, email='zzz@example.com')
        CourseRerunTeamMember.objects.create(batch=batch, email='aaa@example.com')
        emails = list(batch.team_members.values_list('email', flat=True))
        assert emails == ['aaa@example.com', 'zzz@example.com']

    def test_cascade_deleted_with_batch(self, batch):
        CourseRerunTeamMember.objects.create(batch=batch, email='x@example.com')
        batch_id = batch.id
        batch.delete()
        assert CourseRerunTeamMember.objects.filter(batch_id=batch_id).count() == 0
