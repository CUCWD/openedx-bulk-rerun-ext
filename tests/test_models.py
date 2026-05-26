"""
Tests for CourseRerunJob model.
"""
# pylint: disable=redefined-outer-name
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from openedx_bulk_rerun_ext.models import CourseRerunJob

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
