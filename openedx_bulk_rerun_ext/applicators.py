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
    Guarantee the target course has an OrganizationCourse row scoped to its own org.

    rerun_course's clone_instance may record the source org rather than the
    target org, or may leave no association at all when creation is skipped.
    This applicator runs first in apply_course_settings so downstream steps
    (certificates, team access) always see a correct org-course link.
    """
    try:
        # pylint: disable=import-outside-toplevel,import-error
        from organizations.models import Organization, OrganizationCourse
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
    Set the course enrolment mode, activate a certificate configuration, and enable self-generated certificates.

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


def apply_team_access(job, course_key, settings, team_members, requesting_user):
    """
    Add each CAR team member to the target course with the configured roles.

    Studio role is applied via add_instructor (admin) or auth.add_users (staff /
    data_researcher).  Discussion role is applied via the forum role manager when
    the role is not "none".  Each member is also enrolled in the course using the
    batch's configured course_mode so they can access the course in the LMS.
    Members whose email is not found on the platform are skipped with a warning.
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

            # Enroll the team member so they can access the course in the LMS.
            try:
                from common.djangoapps.student.models import CourseEnrollment
                CourseEnrollment.enroll(user, course_key, mode=settings.course_mode)
                _log(job.id, 'ok', f'Enrolled {email} in {course_key} ({settings.course_mode}).')
            except ImportError:
                _log(job.id, 'warn', f'Enrollment skipped for {email}: platform not available.')
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _log(job.id, 'warn', f'Enrollment failed for {email} (non-fatal): {exc}')

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

    * ``copy`` -- copy prerequisite rules from the source course.
    * ``custom`` -- chain every subsection as a prerequisite to the next,
      using settings.gating_min_score and gating_min_completion.
    """
    _log(job.id, 'info', f'Applying lesson gating: mode={settings.gating_mode}')
    try:
        # pylint: disable=import-outside-toplevel
        from opaque_keys.edx.keys import CourseKey
        from openedx.core.lib.gating import api as gating_api

        if settings.gating_mode == 'copy':
            source_key = CourseKey.from_string(job.source_course_key)
            _copy_gating_rules(gating_api, source_key, course_key)
            _log(job.id, 'ok',
                 f'✓ Lesson gating applied: copied prerequisite rules from {job.source_course_key}.')
        elif settings.gating_mode == 'custom':
            _apply_custom_gating(
                gating_api, course_key,
                settings.gating_min_score, settings.gating_min_completion,
                job.created_by.id,
            )
            _log(job.id, 'ok',
                 f'✓ Lesson gating applied: chained all subsections sequentially '
                 f'(min score={settings.gating_min_score}%, '
                 f'min completion={settings.gating_min_completion}%).')
    except ImportError:
        _log(job.id, 'warn', 'Lesson gating skipped: platform not available.')


def _copy_gating_rules(gating_api, source_key, target_key):
    """Copy prerequisite availability and gate assignments from source_key to target_key."""
    from opaque_keys.edx.keys import UsageKey  # pylint: disable=import-outside-toplevel

    # Step 1: Copy prerequisite availability — the "Make this subsection available
    # as a prerequisite to other content" checkbox ('fulfills' relationship).
    for prereq in gating_api.get_prerequisites(source_key):
        source_usage_key = UsageKey.from_string(prereq['block_usage_key'])
        target_usage_key = source_usage_key.map_into_course(target_key)
        gating_api.add_prerequisite(target_key, target_usage_key)

    # Step 2: Copy gate assignments — the "Prerequisite" dropdown ('requires'
    # relationship).  get_prerequisites only covers the fulfills side; we must
    # separately iterate the requires side to reconstruct which subsections are
    # gated behind which prerequisites.
    for milestone in gating_api.find_gating_milestones(source_key, relationship='requires'):
        gated_source_key = UsageKey.from_string(milestone['content_id'])
        gated_target_key = gated_source_key.map_into_course(target_key)

        prereq_source_str, min_score, min_completion = gating_api.get_required_content(
            source_key, gated_source_key
        )
        if not prereq_source_str:
            continue
        prereq_target_key = UsageKey.from_string(prereq_source_str).map_into_course(target_key)

        gating_api.set_required_content(
            target_key, gated_target_key, prereq_target_key,
            '' if min_score is None else min_score,
            '' if min_completion is None else min_completion,
        )


def _apply_custom_gating(gating_api, course_key, min_score, min_completion, user_id):
    """
    Chain every subsection as a sequential prerequisite gate.

    For a course with subsections [A, B, C, D]:
      - A, B, C are marked as available prerequisites ('fulfills').
      - B requires A, C requires B, D requires C ('requires'),
        each with the configured min_score and min_completion thresholds.
    Subsection gating is enabled on the course XBlock if not already set.
    """
    from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel,import-error

    # Fetch the course with depth=2 so chapter and subsection children are loaded.
    store = modulestore()
    course = store.get_course(course_key, depth=2)

    # Collect all subsection locations in course order (chapters → sequentials).
    subsections = [
        subsection.location
        for chapter in course.get_children()
        for subsection in chapter.get_children()
    ]

    # Nothing to gate if there is only one subsection (no "previous" to require).
    if len(subsections) < 2:
        return

    # Enable subsection gating on the course XBlock; without this flag the
    # gating API's @gating_enabled decorator short-circuits all gate checks.
    if not course.enable_subsection_gating:
        course.enable_subsection_gating = True
        store.update_item(course, user_id)

    # Mark every subsection except the last as an available prerequisite so it
    # appears in the "Prerequisite" dropdown for subsequent subsections.
    for location in subsections[:-1]:
        gating_api.add_prerequisite(course_key, location)

    # Wire up the sequential gate: each subsection requires the one before it,
    # with the operator-configured minimum score and completion thresholds.
    for i in range(1, len(subsections)):
        gating_api.set_required_content(
            course_key,
            subsections[i],      # gated subsection
            subsections[i - 1],  # prerequisite (previous subsection)
            min_score,
            min_completion,
        )


def publish_course(job, course_key):
    """
    Publish draft course content and synchronously populate CourseOutlineData for the LMS.

    rerun_course clones content into draft state in the Split modulestore.
    Without an explicit publish the LMS raises CourseOutlineData.DoesNotExist
    and the course-home API returns HTTP 500 for any enrolled learner.

    Two steps are required:
      1. store.publish() promotes the draft blocks to published state and fires
         the COURSE_PUBLISHED signal.  The signal handler enqueues
         update_outline_from_modulestore_task as a Celery task, but that task
         will never execute unless a Celery worker is running and consuming the
         correct queue.  We therefore perform step 2 explicitly.
      2. update_outline_from_modulestore() is called synchronously so that
         CourseOutlineData is guaranteed to be populated before this applicator
         returns, regardless of whether a Celery worker is available.
    """
    _log(job.id, 'info', f'Publishing course content for {course_key}...')

    # Step 1: publish draft content.
    # Import-guard is separate from the publish call so that an ImportError raised
    # by a COURSE_PUBLISHED signal handler (which does its own lazy imports) is not
    # misreported as "platform not available".
    try:
        from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel
    except ImportError:
        _log(job.id, 'warn', 'Course publish skipped: platform not available.')
    else:
        try:
            store = modulestore()
            # make_usage_key('course', 'course') returns the root course block key.
            # Publishing the root in the Split modulestore cascades to all child blocks.
            course_usage_key = course_key.make_usage_key('course', 'course')
            store.publish(course_usage_key, job.created_by.id)
            _log(job.id, 'ok', f'✓ Course published: {course_key}.')
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log(job.id, 'warn', f'Course publish failed (non-fatal): {exc}')

    # Step 2: synchronously populate CourseOutlineData so the LMS course-home API
    # works without depending on a Celery worker processing the COURSE_PUBLISHED signal.
    try:
        # pylint: disable=import-outside-toplevel
        from cms.djangoapps.contentstore.outlines import update_outline_from_modulestore
    except ImportError:
        _log(job.id, 'warn', 'Course outline update skipped: platform not available.')
    else:
        try:
            update_outline_from_modulestore(course_key)
            _log(job.id, 'ok', '✓ Course outline populated for LMS.')
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log(job.id, 'warn', f'Course outline update failed (non-fatal): {exc}')


def enroll_provisioner(job, course_key, requesting_user, settings):
    """
    Enroll the provisioner in the newly created course using the batch-configured course_mode.

    Called immediately after course creation so the provisioner has a valid
    enrollment record before any other settings (certificates, team access) are
    applied.  The enrollment is also updated here so that if remove_provisioner
    later strips the admin role, the student record already reflects the correct
    mode rather than defaulting to 'audit'.
    """
    try:
        from common.djangoapps.student.models import CourseEnrollment  # pylint: disable=import-outside-toplevel
        CourseEnrollment.enroll(requesting_user, course_key, mode=settings.course_mode)
        _log(job.id, 'ok', f'Provisioner enrolled in {course_key} ({settings.course_mode}).')
    except ImportError:
        _log(job.id, 'warn', 'Provisioner enrollment skipped: platform not available.')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(job.id, 'warn', f'Provisioner enrollment failed (non-fatal): {exc}')


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
