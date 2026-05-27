"""
Tests for the bulk-rerun REST API views.

URL patterns under test (ROOT_URLCONF = openedx_bulk_rerun_ext.urls):
  POST /validate/
  POST /jobs/
  GET  /jobs/
  GET  /jobs/<uuid>/
"""
# pylint: disable=redefined-outer-name,unused-argument
import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from openedx_bulk_rerun_ext.models import CourseRerunJob

User = get_user_model()
pytestmark = pytest.mark.django_db

SOURCE_KEY = 'course-v1:CA+FAA-ACS-AM-IA-ACE+DEMO'
TARGET_KEY = 'course-v1:AeroTech+FAA-ACS-AM-IA-ACE+2026_2027'
ALT_TARGET = 'course-v1:SkyLine+FAA-ACS-AM-IA-ACE+2026_2027'


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user():
    return User.objects.create_user(username='staff', password='pw')


@pytest.fixture
def other_user():
    return User.objects.create_user(username='other', password='pw')


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def anon_client():
    return APIClient()


@pytest.fixture
def mock_task():
    """Prevent the Celery task from actually dispatching during view tests."""
    with patch('openedx_bulk_rerun_ext.tasks.run_course_rerun') as mock:
        mock.delay.return_value = MagicMock(id='fake-celery-id')
        yield mock


@pytest.fixture
def existing_job(user):
    return CourseRerunJob.objects.create(
        source_course_key=SOURCE_KEY,
        target_course_key=TARGET_KEY,
        created_by=user,
    )


# ── POST /validate/ ───────────────────────────────────────────────────────────

class TestValidateCourseKeysView:
    """POST /validate/ — key conflict detection before bulk submission."""

    URL = '/validate/'

    def test_requires_authentication(self, anon_client):
        resp = anon_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 401

    def test_missing_keys_field_returns_400(self, auth_client):
        resp = auth_client.post(self.URL, {}, format='json')
        assert resp.status_code == 400

    def test_empty_list_returns_400(self, auth_client):
        resp = auth_client.post(self.URL, {'keys': []}, format='json')
        assert resp.status_code == 400

    def test_too_many_keys_returns_400(self, auth_client):
        keys = [f'course-v1:ORG+NUM{i}+RUN' for i in range(501)]
        resp = auth_client.post(self.URL, {'keys': keys}, format='json')
        assert resp.status_code == 400

    def test_malformed_key_returns_400(self, auth_client):
        resp = auth_client.post(self.URL, {'keys': ['not-a-course-key']}, format='json')
        assert resp.status_code == 400
        assert 'error' in resp.data

    def test_no_conflicts_returns_empty_list(self, auth_client):
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 200
        assert resp.data['existing'] == []

    def test_pending_job_reports_key_as_existing(self, auth_client, existing_job):
        existing_job.status = CourseRerunJob.Status.PENDING
        existing_job.save()
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 200
        assert TARGET_KEY in resp.data['existing']

    def test_running_job_reports_key_as_existing(self, auth_client, existing_job):
        existing_job.status = CourseRerunJob.Status.RUNNING
        existing_job.save()
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert TARGET_KEY in resp.data['existing']

    def test_succeeded_job_reports_key_as_existing(self, auth_client, existing_job):
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert TARGET_KEY in resp.data['existing']

    def test_failed_job_does_not_block(self, auth_client, existing_job):
        # A failed job releases the slot so a new submission can reclaim it.
        existing_job.status = CourseRerunJob.Status.FAILED
        existing_job.save()
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 200
        assert TARGET_KEY not in resp.data['existing']

    def test_only_conflicting_keys_returned(self, auth_client, existing_job):
        existing_job.status = CourseRerunJob.Status.PENDING
        existing_job.save()
        resp = auth_client.post(
            self.URL,
            {'keys': [TARGET_KEY, ALT_TARGET]},
            format='json',
        )
        assert TARGET_KEY in resp.data['existing']
        assert ALT_TARGET not in resp.data['existing']


# ── POST /jobs/ ───────────────────────────────────────────────────────────────

class TestCourseRerunJobCreate:
    """POST /jobs/ — create a rerun job and dispatch the Celery task."""

    URL = '/jobs/'

    def test_requires_authentication(self, anon_client):
        resp = anon_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 401

    def test_invalid_source_key_returns_400(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': 'bad-key', 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 400
        assert 'error' in resp.data

    def test_invalid_target_key_returns_400(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': 'bad-key'},
            format='json',
        )
        assert resp.status_code == 400

    def test_source_equals_target_returns_400(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': SOURCE_KEY},
            format='json',
        )
        assert resp.status_code == 400
        assert 'differ' in resp.data['error']

    def test_duplicate_pending_target_returns_400(self, auth_client, mock_task, existing_job):
        existing_job.status = CourseRerunJob.Status.PENDING
        existing_job.save()
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 400
        assert 'already exists' in resp.data['error']

    def test_duplicate_running_target_returns_400(self, auth_client, mock_task, existing_job):
        existing_job.status = CourseRerunJob.Status.RUNNING
        existing_job.save()
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 400

    def test_duplicate_succeeded_target_returns_400(self, auth_client, mock_task, existing_job):
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 400

    def test_failed_target_allows_retry(self, auth_client, mock_task, existing_job):
        # A previously failed job releases the slot; a new job for the same
        # target must be accepted.
        existing_job.status = CourseRerunJob.Status.FAILED
        existing_job.save()
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': ALT_TARGET},
            format='json',
        )
        assert resp.status_code == 201

    def test_success_returns_201_and_job_fields(self, auth_client, mock_task, user):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 201
        assert resp.data['status'] == 'pending'
        assert resp.data['source_course_key'] == SOURCE_KEY
        assert resp.data['target_course_key'] == TARGET_KEY
        assert resp.data['created_by'] == user.id

    def test_celery_task_id_stored_on_job(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.status_code == 201
        assert resp.data['celery_task_id'] == 'fake-celery-id'

    def test_task_dispatched_once(self, auth_client, mock_task):
        auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        mock_task.delay.assert_called_once()

    def test_optional_bulk_job_id_stored(self, auth_client, mock_task):
        bulk_id = str(uuid.uuid4())
        resp = auth_client.post(
            self.URL,
            {
                'source_course_key': SOURCE_KEY,
                'target_course_key': TARGET_KEY,
                'bulk_job_id': bulk_id,
            },
            format='json',
        )
        assert resp.status_code == 201
        assert resp.data['bulk_job_id'] == bulk_id

    def test_optional_job_type_stored(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {
                'source_course_key': SOURCE_KEY,
                'target_course_key': TARGET_KEY,
                'job_type': 'program_rerun',
            },
            format='json',
        )
        assert resp.status_code == 201
        assert resp.data['job_type'] == 'program_rerun'

    def test_job_type_defaults_to_individual(self, auth_client, mock_task):
        resp = auth_client.post(
            self.URL,
            {'source_course_key': SOURCE_KEY, 'target_course_key': TARGET_KEY},
            format='json',
        )
        assert resp.data['job_type'] == 'individual'


# ── GET /jobs/ ────────────────────────────────────────────────────────────────

class TestCourseRerunJobList:
    """GET /jobs/ — list jobs owned by the requesting user."""

    URL = '/jobs/'

    def test_requires_authentication(self, anon_client):
        resp = anon_client.get(self.URL)
        assert resp.status_code == 401

    def test_returns_empty_list_when_no_jobs(self, auth_client):
        resp = auth_client.get(self.URL)
        assert resp.status_code == 200
        assert resp.data == []

    def test_returns_own_jobs(self, auth_client, existing_job):
        resp = auth_client.get(self.URL)
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]['id'] == str(existing_job.id)

    def test_excludes_other_users_jobs(self, auth_client, other_user):
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=other_user,
        )
        resp = auth_client.get(self.URL)
        assert resp.status_code == 200
        assert resp.data == []

    def test_bulk_job_id_filter(self, auth_client, user):
        group_id = uuid.uuid4()
        j1 = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            bulk_job_id=group_id,
            created_by=user,
        )
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=ALT_TARGET,
            created_by=user,
        )
        resp = auth_client.get(self.URL, {'bulk_job_id': str(group_id)})
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]['id'] == str(j1.id)


# ── GET /jobs/<uuid>/ ─────────────────────────────────────────────────────────

class TestCourseRerunJobDetail:
    """GET /jobs/<uuid>/ — retrieve a single job; 404 for non-owners."""

    def _url(self, job_id):
        return reverse('bulk_rerun:jobs-detail', kwargs={'job_id': job_id})

    def test_requires_authentication(self, anon_client, existing_job):
        resp = anon_client.get(self._url(existing_job.id))
        assert resp.status_code == 401

    def test_returns_own_job(self, auth_client, existing_job):
        resp = auth_client.get(self._url(existing_job.id))
        assert resp.status_code == 200
        assert resp.data['id'] == str(existing_job.id)
        assert resp.data['source_course_key'] == SOURCE_KEY
        assert resp.data['target_course_key'] == TARGET_KEY

    def test_nonexistent_id_returns_404(self, auth_client):
        resp = auth_client.get(self._url(uuid.uuid4()))
        assert resp.status_code == 404

    def test_other_users_job_returns_404(self, auth_client, other_user):
        # Return 404, not 403, to avoid leaking job existence to other users.
        other_job = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=other_user,
        )
        resp = auth_client.get(self._url(other_job.id))
        assert resp.status_code == 404

    def test_all_job_fields_present(self, auth_client, existing_job):
        resp = auth_client.get(self._url(existing_job.id))
        expected_fields = {
            'id', 'bulk_job_id', 'created_by', 'created_at',
            'started_at', 'completed_at', 'status', 'job_type',
            'source_course_key', 'target_course_key',
            'celery_task_id', 'error_message',
        }
        assert expected_fields == set(resp.data.keys())
