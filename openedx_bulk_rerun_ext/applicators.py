"""
Post-rerun settings applicators for openedx_bulk_rerun_ext.

Each function applies one category of operator-configured settings to a newly
created course.  All platform imports are lazy (inside the function body) so the
plugin loads cleanly in test environments that do not have a full edx-platform
install.  Every applicator writes log lines via the _log helper imported from
tasks so there is a single log-writing definition.
"""


def _log(job_id, level, message):
    """Append a structured log line to CourseRerunLog for the given job."""
    from .models import CourseRerunLog  # pylint: disable=import-outside-toplevel
    CourseRerunLog.objects.create(job_id=job_id, level=level, message=message)


def ensure_org_course_association(job, course_key):
    """
    Guarantee that an OrganizationCourse row exists for the target course using
    the target course's own org.

    rerun_course's clone_instance may record the source org rather than the
    target org, or may leave no association at all when creation is skipped.
    This applicator runs first in apply_course_settings so downstream steps
    (certificates, team access) always see a correct org-course link.
    """
    try:
        from organizations.models import Organization, OrganizationCourse  # pylint: disable=import-outside-toplevel
        target_org = Organization.objects.filter(short_name=course_key.org).first()
        if target_org:
            removed, _ = OrganizationCourse.objects.filter(
                course_id=str(course_key),
            ).exclude(organization=target_org).delete()
            if removed:
                _log(job.id, 'info',
                     f'Removed {removed} stale org-course row(s) for {course_key}.')
            _, created = OrganizationCourse.objects.get_or_create(
                course_id=str(course_key),
                organization=target_org,
            )
            if created:
                _log(job.id, 'info',
                     f'Created org-course association: {course_key.org} → {course_key}.')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(job.id, 'warn', f'Org-course association check failed (non-fatal): {exc}')


def apply_scheduling(job, course_key, settings):
    """
    Apply scheduling dates and pacing to the target course.

    Uses CourseDetails.update_from_json which is the correct path for a
    Celery task context — update_course_details requires a full Django request
    object and a pre-loaded course block, neither of which are available here.
    """
    _log(
        job.id, 'info',
        f'Applying scheduling: start={settings.course_start} '
        f'end={settings.course_end} pacing={settings.pacing}',
    )
    try:
        # pylint: disable=import-outside-toplevel
        from openedx.core.djangoapps.models.course_details import CourseDetails
        existing = CourseDetails.fetch(course_key)
        CourseDetails.update_from_json(
            course_key,
            {
                # update_from_json unconditionally reads these two keys; pass
                # existing values so we don't overwrite unrelated course content.
                # Error: Settings application failed: 'overview'
                'overview': existing.overview or '',
                'intro_video': existing.intro_video,

                # Set the new scheduling values from the CAR settings.
                'start_date': settings.course_start.isoformat(),
                'end_date': settings.course_end.isoformat(),
                'enrollment_start': settings.enrollment_start.isoformat(),
                'enrollment_end': settings.enrollment_end.isoformat(),
                'self_paced': settings.pacing == 'self',
            },
            job.created_by,
        )
        _log(job.id, 'ok', '✓ Scheduling applied.')
    except ImportError:
        _log(job.id, 'warn', 'Scheduling skipped: platform not available.')


def apply_certificates(job, course_key, settings):
    """
    Set the course enrolment mode, activate a certificate configuration, and
    enable self-generated certificates.

    Steps:
      1. Update or create the CourseMode row for the target course.
         course_modes in the Studio certificate UI is read at runtime from
         CourseMode.modes_for_course(), so this row is what ties settings.course_mode
         to the certificate context — there is no mode field in the XBlock cert schema.
      2. Ensure a certificate configuration exists on the course XBlock.
         clone_course copies the source's certificates field, so re-runs usually
         inherit a config already; we activate the first one.  If the source had
         none, we create a minimal default so certificate generation has something
         to work with.  Both steps are skipped when create_cert is False.
      3. Enable self-generated certificates via the LMS certificates API.
    """
    _log(
        job.id, 'info',
        f'Applying certificates: mode={settings.course_mode} display={settings.cert_display}',
    )
    # Step 1: Create the CourseMode row so the cert page shows the correct mode.
    mode_applied = False
    try:
        # pylint: disable=import-outside-toplevel
        from common.djangoapps.course_modes.models import CourseMode
        CourseMode.objects.update_or_create(
            course_id=course_key,
            mode_slug=settings.course_mode,
            defaults={
                'mode_display_name': settings.course_mode.capitalize(),
                'expiration_datetime': None,
            },
        )
        mode_applied = True
        _log(job.id, 'ok', f'CourseMode updated to {settings.course_mode}.')
    except ImportError:
        _log(job.id, 'warn', 'CourseMode update skipped: platform not available.')

    if settings.create_cert:
        if not mode_applied:
            _log(job.id, 'warn', 'Certificate configuration skipped: no eligible course mode.')
        else:
            # Step 2: Activate an existing inherited cert config, or create a minimal default.
            try:
                # pylint: disable=import-outside-toplevel
                from xmodule.modulestore.django import modulestore
                store = modulestore()
                course = store.get_course(course_key)
                cert_list = course.certificates.get('certificates', [])
                if cert_list:
                    cert_list[0]['is_active'] = True
                else:
                    course.certificates['certificates'] = [{
                        'id': 1,
                        'name': 'Certificate of Completion',
                        'description': '',
                        'version': 1,
                        'signatories': [],
                        'is_active': True,
                        'editing': False,
                    }]
                store.update_item(course, job.created_by.id)
                _log(job.id, 'ok', 'Certificate configuration activated.')
            except ImportError:
                _log(job.id, 'warn', 'Certificate configuration skipped: platform not available.')

            # Step 3: Enable self-generation so the LMS can issue certs to learners.
            try:
                # pylint: disable=import-outside-toplevel
                from lms.djangoapps.certificates.api import set_cert_generation_enabled
                set_cert_generation_enabled(course_key, True)
                _log(job.id, 'ok', 'Certificate generation enabled.')
            except ImportError:
                _log(job.id, 'warn', 'Certificate generation skipped: platform not available.')

    _log(job.id, 'ok', '✓ Certificates applied.')


def apply_team_access(job, course_key, settings, team_members, requesting_user):  # pylint: disable=unused-argument
    """
    Add each CAR team member to the target course with the configured roles.

    Studio role is applied via add_instructor (admin) or auth.add_users (staff /
    data_researcher).  Discussion role is applied via the forum role manager when
    the role is not "none".  Members whose email is not found on the platform are
    skipped with a warning log line.
    """
    _log(job.id, 'info', 'Assigning team members from CAR...')
    try:
        # pylint: disable=import-outside-toplevel
        from cms.djangoapps.contentstore.utils import add_instructor
        from common.djangoapps.student import auth
        from common.djangoapps.student.roles import CourseStaffRole
        from django.contrib.auth import get_user_model

        User = get_user_model()  # noqa: N806

        for member in team_members:
            email = member.email
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                _log(job.id, 'warn', f'{email} not found in platform — skipped.')
                continue

            studio_role = member.studio_role
            if studio_role == 'admin':
                add_instructor(course_key, requesting_user, user)
            elif studio_role == 'staff':
                auth.add_users(requesting_user, CourseStaffRole(course_key), user)
            elif studio_role == 'data_researcher':
                from common.djangoapps.student.roles import DataResearcherRole
                auth.add_users(requesting_user, DataResearcherRole(course_key), user)

            _apply_discussion_role(job, course_key, user, member.discussion_role)
            _log(job.id, 'ok', f'Added {email} as {studio_role}.')

        _log(job.id, 'ok', '✓ Team access applied.')
    except ImportError:
        _log(job.id, 'warn', 'Team access skipped: platform not available.')


def _apply_discussion_role(job, course_key, user, discussion_role):
    """Add user to the given discussion forum role on the course; skip if role is 'none'."""
    if discussion_role == 'none':
        return
    try:
        # pylint: disable=import-outside-toplevel
        from django_comment_common.models import Role
        from django_comment_common.utils import seed_permissions_roles

        role_map = {
            'discussion_admin': 'Administrator',
            'moderator': 'Moderator',
        }
        platform_role_name = role_map.get(discussion_role)
        if not platform_role_name:
            return
        seed_permissions_roles(course_key)
        role = Role.objects.get(name=platform_role_name, course_id=course_key)
        role.users.add(user)
    except ImportError:
        _log(job.id, 'warn', f'Discussion role assignment skipped for {user.email}: platform not available.')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(job.id, 'warn', f'Discussion role assignment failed for {user.email}: {exc}')


def apply_gating(job, course_key, settings):
    """
    Apply lesson gating rules to the target course.

    Modes:
      copy     — copy prerequisite rules from the source course.
      template — apply a predefined template by gating_template_id.
      custom   — not implemented in Phase 3; logs a warning.
    """
    _log(job.id, 'info', f'Applying lesson gating: mode={settings.gating_mode}')
    if settings.gating_mode == 'custom':
        _log(job.id, 'warn', 'Custom gating mode not implemented in Phase 3 — skipped.')
        return
    try:
        # pylint: disable=import-outside-toplevel
        from opaque_keys.edx.keys import CourseKey
        from openedx.core.lib.gating import api as gating_api

        if settings.gating_mode == 'copy':
            source_key = CourseKey.from_string(job.source_course_key)
            _copy_gating_rules(gating_api, source_key, course_key)
        elif settings.gating_mode == 'template':
            _apply_gating_template(gating_api, course_key, settings.gating_template_id)

        _log(job.id, 'ok', '✓ Lesson gating applied.')
    except ImportError:
        _log(job.id, 'warn', 'Lesson gating skipped: platform not available.')


def _copy_gating_rules(gating_api, source_key, target_key):
    """Copy all prerequisite gating rules from source_key to target_key."""
    prerequisites = gating_api.get_prerequisites(source_key)
    for prereq in prerequisites:
        gating_api.add_prerequisite(target_key, prereq['block_key'])


def _apply_gating_template(gating_api, course_key, template_id):
    """Apply a named gating template to the course; no-op if template_id is empty."""
    if not template_id:
        return
    gating_api.add_prerequisite(course_key, template_id)


def remove_provisioner(job, course_key, requesting_user):
    """
    Remove the provisioner account from the course admin and staff roles.

    Called last, after all other settings have been applied, so the provisioner
    is not accidentally locked out before the course is fully configured.
    There is no public remove_instructor function in Teak; role removal is done
    via auth.remove_users with CourseInstructorRole and CourseStaffRole directly.
    """
    _log(job.id, 'info', 'Removing provisioner from course admin access...')
    try:
        # pylint: disable=import-outside-toplevel
        from common.djangoapps.student import auth
        from common.djangoapps.student.roles import CourseInstructorRole, CourseStaffRole

        auth.remove_users(requesting_user, CourseInstructorRole(course_key), requesting_user)
        auth.remove_users(requesting_user, CourseStaffRole(course_key), requesting_user)
        _log(job.id, 'ok', '✓ Provisioner removed.')
    except ImportError:
        _log(job.id, 'warn', 'Provisioner removal skipped: platform not available.')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(job.id, 'warn', f'Provisioner removal failed (non-fatal): {exc}')
