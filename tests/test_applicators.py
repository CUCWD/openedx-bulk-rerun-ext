"""
Tests for openedx_bulk_rerun_ext.applicators.

Platform modules (openedx.*, lms.*, cms.*, common.*, django_comment_common) are
injected via sys.modules so they do not need to be installed.  opaque-keys IS a
real dependency and is used without mocking.
"""
# pylint: disable=redefined-outer-name,too-many-positional-arguments
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from opaque_keys.edx.keys import CourseKey

from openedx_bulk_rerun_ext.applicators import (
    _apply_discussion_role,
    _copy_gating_rules,
    apply_certificates,
    apply_gating,
    apply_scheduling,
    apply_team_access,
    enroll_provisioner,
    remove_provisioner,
)
from openedx_bulk_rerun_ext.models import BulkRerunBatch, CourseRerunJob, CourseRerunSettings, CourseRerunTeamMember

User = get_user_model()
pytestmark = pytest.mark.django_db

SOURCE_KEY = 'course-v1:CA+FAA-ACS-AM-IA-ACE+DEMO'
TARGET_KEY = 'course-v1:AeroTech+FAA-ACS-AM-IA-ACE+2026_2027'
COURSE_KEY = CourseKey.from_string(TARGET_KEY)


# ── Platform module mocks ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_platform_imports():
    """
    Pre-populate sys.modules with MagicMocks for every platform package used
    by applicators.py.  Yields a SimpleNamespace so tests can inspect calls or
    configure return values.
    """
    mock_course = MagicMock()
    mock_course.certificates = {}
    mock_store = MagicMock()
    mock_store.get_course.return_value = mock_course

    m = SimpleNamespace(
        CourseDetails=MagicMock(),
        CourseMode=MagicMock(),
        set_cert_generation_enabled=MagicMock(),
        add_instructor=MagicMock(),
        auth=MagicMock(),
        CourseEnrollment=MagicMock(),
        CourseStaffRole=MagicMock(),
        CourseInstructorRole=MagicMock(),
        DataResearcherRole=MagicMock(),
        Role=MagicMock(),
        seed_permissions_roles=MagicMock(),
        gating_api=MagicMock(),
        modulestore=MagicMock(return_value=mock_store),
        mock_store=mock_store,
        mock_course=mock_course,
    )
    modules = {
        'openedx': MagicMock(),
        'openedx.core': MagicMock(),
        'openedx.core.djangoapps': MagicMock(),
        'openedx.core.djangoapps.models': MagicMock(),
        'openedx.core.djangoapps.models.course_details': MagicMock(CourseDetails=m.CourseDetails),
        'openedx.core.lib': MagicMock(),
        'openedx.core.lib.gating': MagicMock(api=m.gating_api),
        'lms': MagicMock(),
        'lms.djangoapps': MagicMock(),
        'lms.djangoapps.certificates': MagicMock(),
        'lms.djangoapps.certificates.api': MagicMock(
            set_cert_generation_enabled=m.set_cert_generation_enabled,
        ),
        'cms': MagicMock(),
        'cms.djangoapps': MagicMock(),
        'cms.djangoapps.contentstore': MagicMock(),
        'cms.djangoapps.contentstore.utils': MagicMock(add_instructor=m.add_instructor),
        'common': MagicMock(),
        'common.djangoapps': MagicMock(),
        'common.djangoapps.course_modes': MagicMock(),
        'common.djangoapps.course_modes.models': MagicMock(CourseMode=m.CourseMode),
        'common.djangoapps.student': MagicMock(auth=m.auth),
        'common.djangoapps.student.auth': m.auth,
        'common.djangoapps.student.models': MagicMock(CourseEnrollment=m.CourseEnrollment),
        'common.djangoapps.student.roles': MagicMock(
            CourseStaffRole=m.CourseStaffRole,
            CourseInstructorRole=m.CourseInstructorRole,
            DataResearcherRole=m.DataResearcherRole,
        ),
        'django_comment_common': MagicMock(),
        'django_comment_common.models': MagicMock(Role=m.Role),
        'django_comment_common.utils': MagicMock(seed_permissions_roles=m.seed_permissions_roles),
        'xmodule': MagicMock(),
        'xmodule.modulestore': MagicMock(),
        'xmodule.modulestore.django': MagicMock(modulestore=m.modulestore),
    }
    with patch.dict(sys.modules, modules):
        yield m


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user():
    return User.objects.create_user(username='tester', email='tester@example.com', password='pw')


@pytest.fixture
def batch(user):
    return BulkRerunBatch.objects.create(
        created_by=user,
        mode=BulkRerunBatch.Mode.INDIVIDUAL,
        target_run='2026_2027',
    )


@pytest.fixture
def job(user, batch):
    return CourseRerunJob.objects.create(
        source_course_key=SOURCE_KEY,
        target_course_key=TARGET_KEY,
        created_by=user,
        batch=batch,
        status=CourseRerunJob.Status.RUNNING,
    )


@pytest.fixture
def settings_obj(batch):
    now = timezone.now()
    return CourseRerunSettings.objects.create(
        batch=batch,
        course_start=now,
        course_end=now.replace(year=now.year + 1),
        enrollment_start=now,
        enrollment_end=now.replace(year=now.year + 1),
        course_mode='honor',
        cert_display='early_no_info',
        create_cert=True,
        student_gen_cert=True,
        cert_on_dashboard=True,
        gating_mode='copy',
        remove_provisioner_after=True,
    )


@pytest.fixture
def member_user():
    return User.objects.create_user(username='member', email='member@example.com', password='pw')


# ── apply_scheduling ──────────────────────────────────────────────────────────

class TestApplyScheduling:
    """apply_scheduling delegates to CourseDetails.update_from_json with the configured dates."""

    def test_update_from_json_called_once(self, job, settings_obj, mock_platform_imports):
        apply_scheduling(job, COURSE_KEY, settings_obj)
        mock_platform_imports.CourseDetails.update_from_json.assert_called_once()

    def test_update_from_json_receives_course_key(self, job, settings_obj, mock_platform_imports):
        apply_scheduling(job, COURSE_KEY, settings_obj)
        args = mock_platform_imports.CourseDetails.update_from_json.call_args[0]
        assert args[0] == COURSE_KEY

    def test_update_from_json_includes_start_date(self, job, settings_obj, mock_platform_imports):
        apply_scheduling(job, COURSE_KEY, settings_obj)
        payload = mock_platform_imports.CourseDetails.update_from_json.call_args[0][1]
        assert 'start_date' in payload
        assert payload['start_date'] == settings_obj.course_start.isoformat()

    def test_self_pacing_sends_self_paced_true(self, job, settings_obj, mock_platform_imports):
        settings_obj.pacing = 'self'
        apply_scheduling(job, COURSE_KEY, settings_obj)
        payload = mock_platform_imports.CourseDetails.update_from_json.call_args[0][1]
        assert payload['self_paced'] is True

    def test_instructor_pacing_sends_self_paced_false(self, job, settings_obj, mock_platform_imports):
        settings_obj.pacing = 'instructor'
        apply_scheduling(job, COURSE_KEY, settings_obj)
        payload = mock_platform_imports.CourseDetails.update_from_json.call_args[0][1]
        assert payload['self_paced'] is False

    def test_update_from_json_receives_user(self, job, settings_obj, user, mock_platform_imports):
        apply_scheduling(job, COURSE_KEY, settings_obj)
        args = mock_platform_imports.CourseDetails.update_from_json.call_args[0]
        assert args[2] == user

    def test_success_ok_log_written(self, job, settings_obj):
        apply_scheduling(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Scheduling applied').exists()

    def test_import_error_writes_skip_warn_log(self, job, settings_obj):
        with patch.dict(sys.modules, {'openedx.core.djangoapps.models.course_details': None}):
            apply_scheduling(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Scheduling skipped').exists()

    def test_import_error_does_not_call_update_from_json(self, job, settings_obj, mock_platform_imports):
        with patch.dict(sys.modules, {'openedx.core.djangoapps.models.course_details': None}):
            apply_scheduling(job, COURSE_KEY, settings_obj)
        mock_platform_imports.CourseDetails.update_from_json.assert_not_called()


# ── apply_certificates ────────────────────────────────────────────────────────

class TestApplyCertificates:
    """apply_certificates updates CourseMode and activates certificate generation."""

    def test_course_mode_update_or_create_called(self, job, settings_obj, mock_platform_imports):
        apply_certificates(job, COURSE_KEY, settings_obj)
        mock_platform_imports.CourseMode.objects.update_or_create.assert_called_once_with(
            course_id=COURSE_KEY,
            mode_slug='honor',
            defaults={
                'mode_display_name': 'Honor',
                'expiration_datetime': None,
            },
        )

    def test_cert_generation_enabled_when_create_cert_true(self, job, settings_obj, mock_platform_imports):
        settings_obj.create_cert = True
        apply_certificates(job, COURSE_KEY, settings_obj)
        mock_platform_imports.set_cert_generation_enabled.assert_called_once_with(COURSE_KEY, True)

    def test_cert_generation_not_called_when_create_cert_false(self, job, settings_obj, mock_platform_imports):
        settings_obj.create_cert = False
        apply_certificates(job, COURSE_KEY, settings_obj)
        mock_platform_imports.set_cert_generation_enabled.assert_not_called()

    def test_course_mode_updated_ok_log_written(self, job, settings_obj):
        apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='CourseMode updated').exists()

    def test_certificate_config_activated_ok_log_written(self, job, settings_obj):
        apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Certificate configuration activated').exists()

    def test_certificate_generation_enabled_ok_log_written(self, job, settings_obj):
        apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Certificate generation enabled').exists()

    def test_certificates_applied_ok_log_written(self, job, settings_obj):
        apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Certificates applied').exists()

    def test_store_update_item_called_when_create_cert_true(self, job, settings_obj, mock_platform_imports):
        settings_obj.create_cert = True
        apply_certificates(job, COURSE_KEY, settings_obj)
        mock_platform_imports.mock_store.update_item.assert_called_once()

    def test_existing_cert_is_activated_not_replaced(self, job, settings_obj, mock_platform_imports):
        existing = {'id': 99, 'name': 'Existing Cert', 'is_active': False}
        mock_platform_imports.mock_course.certificates = {'certificates': [existing]}
        apply_certificates(job, COURSE_KEY, settings_obj)
        assert existing['is_active'] is True
        certs = mock_platform_imports.mock_course.certificates['certificates']
        assert len(certs) == 1

    def test_default_cert_created_when_none_inherited(self, job, settings_obj, mock_platform_imports):
        mock_platform_imports.mock_course.certificates = {}
        apply_certificates(job, COURSE_KEY, settings_obj)
        certs = mock_platform_imports.mock_course.certificates['certificates']
        assert len(certs) == 1
        assert certs[0]['is_active'] is True
        assert certs[0]['name'] == 'Certificate of Completion'

    def test_course_mode_import_error_logs_skip(self, job, settings_obj):
        with patch.dict(sys.modules, {'common.djangoapps.course_modes.models': None}):
            apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='CourseMode update skipped').exists()

    def test_cert_config_skipped_when_no_mode_applied(self, job, settings_obj, mock_platform_imports):
        with patch.dict(sys.modules, {'common.djangoapps.course_modes.models': None}):
            apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='no eligible course mode').exists()
        mock_platform_imports.mock_store.update_item.assert_not_called()
        mock_platform_imports.set_cert_generation_enabled.assert_not_called()

    def test_xmodule_import_error_logs_skip(self, job, settings_obj):
        with patch.dict(sys.modules, {'xmodule.modulestore.django': None}):
            apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Certificate configuration skipped').exists()

    def test_cert_api_import_error_logs_skip(self, job, settings_obj):
        with patch.dict(sys.modules, {'lms.djangoapps.certificates.api': None}):
            apply_certificates(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Certificate generation skipped').exists()

    def test_cert_api_import_error_does_not_prevent_mode_update(self, job, settings_obj, mock_platform_imports):
        with patch.dict(sys.modules, {'lms.djangoapps.certificates.api': None}):
            apply_certificates(job, COURSE_KEY, settings_obj)
        mock_platform_imports.CourseMode.objects.update_or_create.assert_called_once()


# ── apply_team_access ─────────────────────────────────────────────────────────

class TestApplyTeamAccess:
    """apply_team_access assigns studio and discussion roles to each CAR team member."""

    def _make_member(self, batch, email, studio_role, discussion_role='none'):
        return CourseRerunTeamMember.objects.create(
            batch=batch,
            email=email,
            studio_role=studio_role,
            discussion_role=discussion_role,
        )

    def test_add_instructor_called_for_admin_role(
        self, job, settings_obj, user, batch, member_user, mock_platform_imports,
    ):
        member = self._make_member(batch, member_user.email, 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        mock_platform_imports.add_instructor.assert_called_once_with(COURSE_KEY, user, member_user)

    def test_add_users_called_for_staff_role(
        self, job, settings_obj, user, batch, member_user, mock_platform_imports,
    ):
        member = self._make_member(batch, member_user.email, 'staff')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        mock_platform_imports.auth.add_users.assert_called_once()
        args = mock_platform_imports.auth.add_users.call_args[0]
        assert args[0] == user
        assert args[2] == member_user

    def test_add_users_called_for_data_researcher_role(
        self, job, settings_obj, user, batch, member_user, mock_platform_imports,
    ):
        member = self._make_member(batch, member_user.email, 'data_researcher')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        mock_platform_imports.auth.add_users.assert_called_once()

    def test_unknown_email_skips_with_warn_log(self, job, settings_obj, user, batch):
        member = self._make_member(batch, 'ghost@example.com', 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        assert job.logs.filter(level='warn', message__icontains='ghost@example.com').exists()

    def test_added_member_ok_log_written(
        self, job, settings_obj, user, batch, member_user,
    ):
        member = self._make_member(batch, member_user.email, 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        assert job.logs.filter(level='ok', message__icontains='member@example.com').exists()

    def test_team_access_applied_ok_log_written(
        self, job, settings_obj, user, batch, member_user,
    ):
        member = self._make_member(batch, member_user.email, 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        assert job.logs.filter(level='ok', message__icontains='Team access applied').exists()

    def test_empty_team_completes_without_error(self, job, settings_obj, user):
        apply_team_access(job, COURSE_KEY, settings_obj, [], user)
        assert job.logs.filter(level='ok', message__icontains='Team access applied').exists()

    def test_member_enrolled_with_batch_course_mode(
        self, job, settings_obj, user, batch, member_user, mock_platform_imports,
    ):
        member = self._make_member(batch, member_user.email, 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        mock_platform_imports.CourseEnrollment.enroll.assert_called_once_with(
            member_user, COURSE_KEY, mode=settings_obj.course_mode,
        )
        assert job.logs.filter(level='ok', message__icontains='Enrolled member@example.com').exists()

    def test_enrollment_failure_is_non_fatal(
        self, job, settings_obj, user, batch, member_user, mock_platform_imports,
    ):
        mock_platform_imports.CourseEnrollment.enroll.side_effect = RuntimeError('enroll error')
        member = self._make_member(batch, member_user.email, 'admin')
        apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        assert job.logs.filter(level='warn', message__icontains='Enrollment failed').exists()

    def test_enrollment_import_error_logs_skip(self, job, settings_obj, user, batch, member_user):
        member = self._make_member(batch, member_user.email, 'admin')
        with patch.dict(sys.modules, {'common.djangoapps.student.models': None}):
            apply_team_access(job, COURSE_KEY, settings_obj, [member], user)
        assert job.logs.filter(level='warn', message__icontains='Enrollment skipped').exists()

    def test_import_error_logs_skip(self, job, settings_obj, user):
        with patch.dict(sys.modules, {'cms.djangoapps.contentstore.utils': None}):
            apply_team_access(job, COURSE_KEY, settings_obj, [], user)
        assert job.logs.filter(level='warn', message__icontains='Team access skipped').exists()


# ── _apply_discussion_role ────────────────────────────────────────────────────

class TestApplyDiscussionRole:
    """_apply_discussion_role assigns the correct forum role to a platform user."""

    def test_none_role_does_not_touch_platform(self, job, member_user, mock_platform_imports):
        _apply_discussion_role(job, COURSE_KEY, member_user, 'none')
        mock_platform_imports.seed_permissions_roles.assert_not_called()
        mock_platform_imports.Role.objects.get.assert_not_called()

    def test_discussion_admin_queries_administrator_role(self, job, member_user, mock_platform_imports):
        _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        mock_platform_imports.Role.objects.get.assert_called_once_with(
            name='Administrator', course_id=COURSE_KEY,
        )

    def test_moderator_queries_moderator_role(self, job, member_user, mock_platform_imports):
        _apply_discussion_role(job, COURSE_KEY, member_user, 'moderator')
        mock_platform_imports.Role.objects.get.assert_called_once_with(
            name='Moderator', course_id=COURSE_KEY,
        )

    def test_user_added_to_role(self, job, member_user, mock_platform_imports):
        role_instance = MagicMock()
        mock_platform_imports.Role.objects.get.return_value = role_instance
        _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        role_instance.users.add.assert_called_once_with(member_user)

    def test_seed_permissions_called_before_role_lookup(self, job, member_user, mock_platform_imports):
        _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        mock_platform_imports.seed_permissions_roles.assert_called_once_with(COURSE_KEY)

    def test_unknown_discussion_role_does_not_call_role_lookup(
        self, job, member_user, mock_platform_imports,
    ):
        _apply_discussion_role(job, COURSE_KEY, member_user, 'unknown_role')
        mock_platform_imports.Role.objects.get.assert_not_called()

    def test_import_error_writes_warn_log(self, job, member_user):
        with patch.dict(sys.modules, {'django_comment_common.models': None}):
            _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        assert job.logs.filter(
            level='warn', message__icontains='Discussion role assignment skipped',
        ).exists()

    def test_role_lookup_exception_writes_warn_log(self, job, member_user, mock_platform_imports):
        mock_platform_imports.Role.objects.get.side_effect = RuntimeError('role missing')
        _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        assert job.logs.filter(
            level='warn', message__icontains='Discussion role assignment failed',
        ).exists()

    def test_role_lookup_exception_is_non_fatal(self, job, member_user, mock_platform_imports):
        mock_platform_imports.Role.objects.get.side_effect = RuntimeError('role missing')
        _apply_discussion_role(job, COURSE_KEY, member_user, 'discussion_admin')
        # no exception propagated — function returns normally


# ── apply_gating ──────────────────────────────────────────────────────────────

class TestApplyGating:
    """apply_gating delegates to _copy_gating_rules when mode is 'copy'."""

    def test_copy_mode_calls_get_prerequisites(self, job, settings_obj, mock_platform_imports):
        settings_obj.gating_mode = 'copy'
        mock_platform_imports.gating_api.get_prerequisites.return_value = []
        apply_gating(job, COURSE_KEY, settings_obj)
        mock_platform_imports.gating_api.get_prerequisites.assert_called_once()

    def test_success_ok_log_written(self, job, settings_obj, mock_platform_imports):
        settings_obj.gating_mode = 'copy'
        mock_platform_imports.gating_api.get_prerequisites.return_value = []
        apply_gating(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Lesson gating applied').exists()

    def test_copy_mode_passes_source_key(self, job, settings_obj, mock_platform_imports):
        settings_obj.gating_mode = 'copy'
        mock_platform_imports.gating_api.get_prerequisites.return_value = []
        apply_gating(job, COURSE_KEY, settings_obj)
        source_key = CourseKey.from_string(SOURCE_KEY)
        mock_platform_imports.gating_api.get_prerequisites.assert_called_once_with(source_key)

    def test_import_error_writes_skip_warn_log(self, job, settings_obj):
        settings_obj.gating_mode = 'copy'
        with patch.dict(sys.modules, {'openedx.core.lib.gating': None}):
            apply_gating(job, COURSE_KEY, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Lesson gating skipped').exists()


# ── _copy_gating_rules ────────────────────────────────────────────────────────

class TestCopyGatingRules:
    """_copy_gating_rules iterates prerequisites and registers each on the target."""

    def test_add_prerequisite_called_per_prereq(self, mock_platform_imports):
        gating_api = mock_platform_imports.gating_api
        source_key = CourseKey.from_string(SOURCE_KEY)
        gating_api.get_prerequisites.return_value = [
            {'block_key': 'block-v1:ORG+CS+RUN+type@unit+block@aaa'},
            {'block_key': 'block-v1:ORG+CS+RUN+type@unit+block@bbb'},
        ]
        _copy_gating_rules(gating_api, source_key, COURSE_KEY)
        assert gating_api.add_prerequisite.call_count == 2

    def test_add_prerequisite_called_with_target_key(self, mock_platform_imports):
        gating_api = mock_platform_imports.gating_api
        source_key = CourseKey.from_string(SOURCE_KEY)
        gating_api.get_prerequisites.return_value = [
            {'block_key': 'block-v1:ORG+CS+RUN+type@unit+block@aaa'},
        ]
        _copy_gating_rules(gating_api, source_key, COURSE_KEY)
        gating_api.add_prerequisite.assert_called_once_with(
            COURSE_KEY, 'block-v1:ORG+CS+RUN+type@unit+block@aaa',
        )

    def test_no_prerequisites_means_no_add_calls(self, mock_platform_imports):
        gating_api = mock_platform_imports.gating_api
        source_key = CourseKey.from_string(SOURCE_KEY)
        gating_api.get_prerequisites.return_value = []
        _copy_gating_rules(gating_api, source_key, COURSE_KEY)
        gating_api.add_prerequisite.assert_not_called()


# ── remove_provisioner ────────────────────────────────────────────────────────

class TestEnrollProvisioner:
    """enroll_provisioner creates/updates the provisioner enrollment immediately after course creation."""

    def test_enroll_called_with_batch_course_mode(self, job, settings_obj, user, mock_platform_imports):
        enroll_provisioner(job, COURSE_KEY, user, settings_obj)
        mock_platform_imports.CourseEnrollment.enroll.assert_called_once_with(
            user, COURSE_KEY, mode=settings_obj.course_mode,
        )

    def test_success_ok_log_written(self, job, settings_obj, user):
        enroll_provisioner(job, COURSE_KEY, user, settings_obj)
        assert job.logs.filter(level='ok', message__icontains='Provisioner enrolled').exists()

    def test_import_error_logs_skip(self, job, settings_obj, user):
        with patch.dict(sys.modules, {'common.djangoapps.student.models': None}):
            enroll_provisioner(job, COURSE_KEY, user, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Provisioner enrollment skipped').exists()

    def test_exception_is_non_fatal(self, job, settings_obj, user, mock_platform_imports):
        mock_platform_imports.CourseEnrollment.enroll.side_effect = RuntimeError('enroll error')
        enroll_provisioner(job, COURSE_KEY, user, settings_obj)
        assert job.logs.filter(level='warn', message__icontains='Provisioner enrollment failed').exists()


class TestRemoveProvisioner:
    """remove_provisioner strips course admin and staff roles from the requesting user."""

    def test_remove_users_called_twice(self, job, user, mock_platform_imports):
        remove_provisioner(job, COURSE_KEY, user)
        assert mock_platform_imports.auth.remove_users.call_count == 2

    def test_remove_users_first_arg_is_requesting_user(self, job, user, mock_platform_imports):
        remove_provisioner(job, COURSE_KEY, user)
        for call in mock_platform_imports.auth.remove_users.call_args_list:
            assert call[0][0] == user

    def test_remove_users_last_arg_is_requesting_user(self, job, user, mock_platform_imports):
        remove_provisioner(job, COURSE_KEY, user)
        for call in mock_platform_imports.auth.remove_users.call_args_list:
            assert call[0][2] == user

    def test_success_ok_log_written(self, job, user):
        remove_provisioner(job, COURSE_KEY, user)
        assert job.logs.filter(level='ok', message__icontains='Provisioner removed').exists()

    def test_import_error_writes_skip_warn_log(self, job, user):
        with patch.dict(sys.modules, {'common.djangoapps.student.roles': None}):
            remove_provisioner(job, COURSE_KEY, user)
        assert job.logs.filter(level='warn', message__icontains='Provisioner removal skipped').exists()

    def test_non_import_exception_is_non_fatal(self, job, user, mock_platform_imports):
        mock_platform_imports.auth.remove_users.side_effect = PermissionError('denied')
        remove_provisioner(job, COURSE_KEY, user)
        assert job.logs.filter(level='warn', message__icontains='Provisioner removal failed').exists()

    def test_non_import_exception_does_not_propagate(self, job, user, mock_platform_imports):
        mock_platform_imports.auth.remove_users.side_effect = PermissionError('denied')
        remove_provisioner(job, COURSE_KEY, user)
        # no exception raised — function returns normally
