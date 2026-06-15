"""
Tests for the bulk-rerun REST API views.

URL patterns under test (ROOT_URLCONF = openedx_bulk_rerun_ext.urls):
  POST /validate/
  POST /jobs/
  GET  /jobs/
  GET  /jobs/<uuid>/
  POST /batches/
  GET  /batches/<uuid>/
  GET  /jobs/<uuid>/logs/
"""
# pylint: disable=redefined-outer-name,unused-argument
import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from openedx_bulk_rerun_ext.models import BulkRerunBatch, CourseRerunJob, CourseRerunLog, CourseRerunSettings

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
def mock_batch_task():
    """Prevent dispatch_batch_rerun from actually dispatching during view tests."""
    with patch('openedx_bulk_rerun_ext.tasks.dispatch_batch_rerun') as mock:
        mock.delay.return_value = MagicMock(id='fake-batch-celery-id')
        yield mock


@pytest.fixture
def existing_job(user):
    return CourseRerunJob.objects.create(
        source_course_key=SOURCE_KEY,
        target_course_key=TARGET_KEY,
        created_by=user,
    )


@pytest.fixture
def existing_batch(user):
    return BulkRerunBatch.objects.create(
        created_by=user,
        mode=BulkRerunBatch.Mode.INDIVIDUAL,
        target_run='2026_2027',
    )


# ── Phase 1: POST /validate/ ──────────────────────────────────────────────────

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

    def test_succeeded_job_alone_does_not_report_key_as_existing(self, auth_client, existing_job):
        # A SUCCEEDED job whose course was subsequently deleted must not be
        # flagged as a conflict — only the modulestore is authoritative.
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 200
        assert TARGET_KEY not in resp.data['existing']

    def test_succeeded_job_with_live_course_reports_key_as_existing(self, auth_client, existing_job):
        # If the modulestore confirms the course still exists, it must still be flagged.
        import sys
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        mock_store = MagicMock()
        mock_store.has_course.return_value = True
        fake_django_mod = MagicMock()
        fake_django_mod.modulestore = MagicMock(return_value=mock_store)
        fake_modules = {
            'xmodule': MagicMock(),
            'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': fake_django_mod,
        }
        with patch.dict(sys.modules, fake_modules):
            resp = auth_client.post(self.URL, {'keys': [TARGET_KEY]}, format='json')
        assert resp.status_code == 200
        assert TARGET_KEY in resp.data['existing']

    def test_failed_job_does_not_block(self, auth_client, existing_job):
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


# ── Phase 1: POST /jobs/ ──────────────────────────────────────────────────────

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


# ── Phase 1: GET /jobs/ ───────────────────────────────────────────────────────

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


# ── Phase 1: GET /jobs/<uuid>/ ────────────────────────────────────────────────

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
            'batch', 'position', 'settings_applied',
        }
        assert expected_fields == set(resp.data.keys())


# ── Phase 2: POST /batches/ ───────────────────────────────────────────────────

class TestBulkRerunBatchCreate:
    """POST /batches/ — create a batch and fan out child jobs."""

    URL = '/batches/'

    _DEFAULT_SETTINGS = {
        'course_start': '2025-08-01T00:00:00Z',
        'course_end': '2026-07-31T23:59:59Z',
        'enrollment_start': '2025-08-01T00:00:00Z',
        'enrollment_end': '2026-07-31T23:59:59Z',
        'pacing': 'instructor',
        'course_mode': 'honor',
        'cert_display': 'early_no_info',
        'create_cert': True,
        'student_gen_cert': True,
        'cert_on_dashboard': True,
        'gating_mode': 'disabled',
        'gating_template_id': '',
        'remove_provisioner_after': True,
    }

    def _payload(self, **overrides):  # pylint: disable=missing-function-docstring
        data = {
            'mode': 'individual',
            'is_dry_run': False,
            'target_run': '2026_2027',
            'courses': [
                {
                    'source_course_key': SOURCE_KEY,
                    'target_course_key': TARGET_KEY,
                    'job_type': 'individual',
                }
            ],
            'settings': self._DEFAULT_SETTINGS,
            'team_members': [],
        }
        data.update(overrides)
        return data

    def test_requires_authentication(self, anon_client):
        resp = anon_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 401

    def test_returns_202(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 202

    def test_response_contains_batch_id_and_jobs(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert 'batch_id' in resp.data
        assert resp.data['total_jobs'] == 1
        assert len(resp.data['jobs']) == 1
        assert resp.data['jobs'][0]['target_course_key'] == TARGET_KEY

    def test_batch_row_created_in_db(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert BulkRerunBatch.objects.filter(id=resp.data['batch_id']).exists()

    def test_job_rows_created_in_db(self, auth_client, mock_batch_task):
        auth_client.post(self.URL, self._payload(), format='json')
        assert CourseRerunJob.objects.filter(target_course_key=TARGET_KEY).exists()

    def test_fan_out_task_dispatched(self, auth_client, mock_batch_task):
        auth_client.post(self.URL, self._payload(), format='json')
        mock_batch_task.delay.assert_called_once()

    def test_invalid_course_key_returns_400(self, auth_client, mock_batch_task):
        payload = self._payload()
        payload['courses'][0]['target_course_key'] = 'bad-key'
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 400

    def test_source_equals_target_returns_400(self, auth_client, mock_batch_task):
        payload = self._payload()
        payload['courses'][0]['target_course_key'] = SOURCE_KEY
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 400

    def test_duplicate_targets_within_batch_returns_400(self, auth_client, mock_batch_task):
        payload = self._payload()
        payload['courses'].append({
            'source_course_key': SOURCE_KEY,
            'target_course_key': TARGET_KEY,
            'job_type': 'individual',
        })
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 400

    def test_active_target_key_blocked(self, auth_client, mock_batch_task, existing_job):
        existing_job.status = CourseRerunJob.Status.RUNNING
        existing_job.save()
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 400
        assert 'keys' in resp.data

    def test_succeeded_job_for_deleted_course_allows_new_batch(self, auth_client, mock_batch_task, existing_job):
        # A SUCCEEDED job whose course was subsequently deleted must not block
        # a new batch submission — only the modulestore is authoritative.
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 202

    def test_succeeded_job_with_live_course_blocks_new_batch(self, auth_client, mock_batch_task, existing_job):
        import sys
        existing_job.status = CourseRerunJob.Status.SUCCEEDED
        existing_job.save()
        mock_store = MagicMock()
        mock_store.has_course.return_value = True
        fake_django_mod = MagicMock()
        fake_django_mod.modulestore = MagicMock(return_value=mock_store)
        fake_modules = {
            'xmodule': MagicMock(),
            'xmodule.modulestore': MagicMock(),
            'xmodule.modulestore.django': fake_django_mod,
        }
        with patch.dict(sys.modules, fake_modules):
            resp = auth_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 400
        assert TARGET_KEY in resp.data['keys']

    def test_failed_target_allows_new_batch(self, auth_client, mock_batch_task, existing_job):
        existing_job.status = CourseRerunJob.Status.FAILED
        existing_job.save()
        payload = self._payload()
        payload['courses'][0]['target_course_key'] = ALT_TARGET
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 202

    def test_dry_run_flag_stored(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(is_dry_run=True), format='json')
        assert resp.status_code == 202
        batch = BulkRerunBatch.objects.get(id=resp.data['batch_id'])
        assert batch.is_dry_run is True

    def test_zero_courses_returns_400(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(courses=[]), format='json')
        assert resp.status_code == 400

    def test_prog_id_stored_when_provided(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(prog_id='faa'), format='json')
        assert resp.status_code == 202
        batch = BulkRerunBatch.objects.get(id=resp.data['batch_id'])
        assert batch.prog_id == 'faa'


# ── Phase 2: GET /batches/<uuid>/ ─────────────────────────────────────────────

class TestBulkRerunBatchDetail:
    """GET /batches/<uuid>/ — poll full batch status with nested jobs."""

    def _url(self, batch_id):
        return reverse('bulk_rerun:batches-detail', kwargs={'batch_id': batch_id})

    def test_requires_authentication(self, anon_client, existing_batch):
        resp = anon_client.get(self._url(existing_batch.id))
        assert resp.status_code == 401

    def test_returns_batch_fields(self, auth_client, existing_batch):
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.status_code == 200
        assert str(existing_batch.id) == resp.data['id']
        assert resp.data['status'] == 'pending'
        assert resp.data['mode'] == 'individual'

    def test_returns_jobs_list(self, auth_client, user, existing_batch):
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=user,
            batch=existing_batch,
        )
        resp = auth_client.get(self._url(existing_batch.id))
        assert len(resp.data['jobs']) == 1
        assert resp.data['jobs'][0]['target_course_key'] == TARGET_KEY

    def test_total_done_failed_counts_in_response(self, auth_client, user, existing_batch):
        j = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=user,
            batch=existing_batch,
        )
        j.status = CourseRerunJob.Status.SUCCEEDED
        j.save()
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.data['total_jobs'] == 1
        assert resp.data['done_jobs'] == 1
        assert resp.data['failed_jobs'] == 0

    def test_nonexistent_batch_returns_404(self, auth_client):
        resp = auth_client.get(self._url(uuid.uuid4()))
        assert resp.status_code == 404

    def test_other_users_batch_returns_404(self, auth_client, other_user):
        other_batch = BulkRerunBatch.objects.create(
            created_by=other_user,
            mode=BulkRerunBatch.Mode.INDIVIDUAL,
            target_run='2026_2027',
        )
        resp = auth_client.get(self._url(other_batch.id))
        assert resp.status_code == 404

    def test_elapsed_seconds_present_for_completed_job(self, auth_client, user, existing_batch):
        j = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=user,
            batch=existing_batch,
        )
        now = timezone.now()
        j.started_at = now - datetime.timedelta(seconds=5)
        j.completed_at = now
        j.status = CourseRerunJob.Status.SUCCEEDED
        j.save()
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.data['jobs'][0]['elapsed_seconds'] is not None
        assert resp.data['jobs'][0]['elapsed_seconds'] >= 5.0


# ── Phase 2: GET /jobs/<uuid>/logs/ ──────────────────────────────────────────

class TestCourseRerunJobLogsView:
    """GET /jobs/<uuid>/logs/ — retrieve log lines, with optional ?since= filter."""

    def _url(self, job_id):
        return reverse('bulk_rerun:jobs-logs', kwargs={'job_id': job_id})

    def test_requires_authentication(self, anon_client, existing_job):
        resp = anon_client.get(self._url(existing_job.id))
        assert resp.status_code == 401

    def test_returns_empty_logs_for_new_job(self, auth_client, existing_job):
        resp = auth_client.get(self._url(existing_job.id))
        assert resp.status_code == 200
        assert resp.data['job_id'] == str(existing_job.id)
        assert resp.data['logs'] == []

    def test_returns_all_log_lines(self, auth_client, existing_job):
        CourseRerunLog.objects.create(job=existing_job, level='info', message='started')
        CourseRerunLog.objects.create(job=existing_job, level='ok', message='done')
        resp = auth_client.get(self._url(existing_job.id))
        assert len(resp.data['logs']) == 2

    def test_log_fields_present(self, auth_client, existing_job):
        CourseRerunLog.objects.create(job=existing_job, level='info', message='hello')
        resp = auth_client.get(self._url(existing_job.id))
        log = resp.data['logs'][0]
        assert set(log.keys()) == {'id', 'level', 'message', 'created_at'}

    def test_since_filter_returns_only_newer_lines(self, auth_client, existing_job):
        l1 = CourseRerunLog.objects.create(job=existing_job, message='line 1')
        CourseRerunLog.objects.create(job=existing_job, message='line 2')
        CourseRerunLog.objects.create(job=existing_job, message='line 3')
        resp = auth_client.get(self._url(existing_job.id), {'since': l1.id})
        messages = [log['message'] for log in resp.data['logs']]
        assert messages == ['line 2', 'line 3']

    def test_since_filter_invalid_value_returns_400(self, auth_client, existing_job):
        resp = auth_client.get(self._url(existing_job.id), {'since': 'not-an-int'})
        assert resp.status_code == 400

    def test_job_status_included_in_response(self, auth_client, existing_job):
        existing_job.status = CourseRerunJob.Status.RUNNING
        existing_job.save()
        resp = auth_client.get(self._url(existing_job.id))
        assert resp.data['job_status'] == 'running'

    def test_nonexistent_job_returns_404(self, auth_client):
        resp = auth_client.get(self._url(uuid.uuid4()))
        assert resp.status_code == 404

    def test_other_users_job_returns_404(self, auth_client, other_user):
        other_job = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY,
            target_course_key=TARGET_KEY,
            created_by=other_user,
        )
        resp = auth_client.get(self._url(other_job.id))
        assert resp.status_code == 404


# ── Phase 3: POST /batches/ — settings and team members ──────────────────────

class TestBulkRerunBatchCreatePhase3(TestBulkRerunBatchCreate):
    """POST /batches/ creates CourseRerunSettings and CourseRerunTeamMember rows."""

    def test_settings_row_created_in_db(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(), format='json')
        assert resp.status_code == 202
        batch = BulkRerunBatch.objects.get(id=resp.data['batch_id'])
        assert CourseRerunSettings.objects.filter(batch=batch).exists()

    def test_settings_fields_stored_correctly(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(), format='json')
        batch = BulkRerunBatch.objects.get(id=resp.data['batch_id'])
        s = batch.settings
        assert s.pacing == 'instructor'
        assert s.course_mode == 'honor'
        assert s.gating_mode == 'disabled'
        assert s.remove_provisioner_after is True

    def test_team_member_rows_created_in_db(self, auth_client, mock_batch_task):
        payload = self._payload(team_members=[
            {'email': 'inst@example.com', 'studio_role': 'admin', 'discussion_role': 'discussion_admin'},
        ])
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 202
        batch = BulkRerunBatch.objects.get(id=resp.data['batch_id'])
        assert batch.team_members.filter(email='inst@example.com').exists()

    def test_empty_team_members_list_is_accepted(self, auth_client, mock_batch_task):
        resp = auth_client.post(self.URL, self._payload(team_members=[]), format='json')
        assert resp.status_code == 202

    def test_missing_settings_returns_400(self, auth_client, mock_batch_task):
        payload = self._payload()
        del payload['settings']
        resp = auth_client.post(self.URL, payload, format='json')
        assert resp.status_code == 400

    def test_start_after_end_returns_400(self, auth_client, mock_batch_task):
        bad_settings = dict(self._DEFAULT_SETTINGS)
        bad_settings['course_start'] = '2027-01-01T00:00:00Z'
        bad_settings['course_end'] = '2026-01-01T00:00:00Z'
        resp = auth_client.post(self.URL, self._payload(settings=bad_settings), format='json')
        assert resp.status_code == 400

    def test_enrollment_start_after_course_start_returns_400(self, auth_client, mock_batch_task):
        bad_settings = dict(self._DEFAULT_SETTINGS)
        bad_settings['enrollment_start'] = '2026-01-01T00:00:00Z'
        resp = auth_client.post(self.URL, self._payload(settings=bad_settings), format='json')
        assert resp.status_code == 400


# ── Phase 3: GET /batches/<uuid>/ — settings_applied_count ───────────────────

class TestBulkRerunBatchDetailPhase3:
    """GET /batches/<uuid>/ includes settings_applied_count in the response."""

    def _url(self, batch_id):
        return reverse('bulk_rerun:batches-detail', kwargs={'batch_id': batch_id})

    def test_settings_applied_count_in_response(self, auth_client, existing_batch):
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.status_code == 200
        assert 'settings_applied_count' in resp.data

    def test_settings_applied_count_is_zero_initially(self, auth_client, user, existing_batch):
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY, target_course_key=TARGET_KEY,
            created_by=user, batch=existing_batch,
        )
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.data['settings_applied_count'] == 0

    def test_settings_applied_count_increments(self, auth_client, user, existing_batch):
        j = CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY, target_course_key=TARGET_KEY,
            created_by=user, batch=existing_batch,
        )
        j.settings_applied = True
        j.save()
        resp = auth_client.get(self._url(existing_batch.id))
        assert resp.data['settings_applied_count'] == 1

    def test_settings_applied_in_job_fields(self, auth_client, user, existing_batch):
        CourseRerunJob.objects.create(
            source_course_key=SOURCE_KEY, target_course_key=TARGET_KEY,
            created_by=user, batch=existing_batch,
        )
        resp = auth_client.get(self._url(existing_batch.id))
        assert 'settings_applied' in resp.data['jobs'][0]
