"""Reconcile historical rerun courses with their batch certificate settings."""
import json
import logging
from collections import Counter
from itertools import groupby

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from openedx_bulk_rerun_ext.models import CourseRerunJob, CourseRerunSettings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Produce an ordered JSON-lines audit, optionally applying the repairs."""

    help = 'Backfill course certificate display behavior from historical bulk rerun settings.'

    def add_arguments(self, parser):
        """Require an explicit writer identity only when applying changes."""
        parser.add_argument('--apply', action='store_true', help='Save changes; otherwise preview only.')
        parser.add_argument('--username', help='Existing Django username for modulestore edit attribution.')
        parser.add_argument('--org', help='Only target courses with this exact, case-sensitive organization key.')

    # Keep the ordered audit, repair, and verification workflow together.
    def handle(self, *args, **options):  # pylint: disable=too-many-statements
        """Visit course keys alphabetically and report every decision and save."""
        # pylint: disable=import-outside-toplevel,import-error
        from openedx.core.djangoapps.models.course_details import CourseDetails
        from xmodule.data import CertificatesDisplayBehaviors
        from xmodule.modulestore.django import modulestore

        apply = options['apply']
        username = options.get('username')
        user_id = None
        org = options.get('org')
        if org is not None and not org.strip():
            raise CommandError('--org must not be empty.')
        if apply and not username:
            raise CommandError('--apply requires --username identifying an existing Django user.')
        if username:
            user = get_user_model().objects.filter(username=username).first()
            if user is None:
                raise CommandError(f'No Django user found with username {username!r}.')
            # The modulestore API still requires the resolved user's primary key.
            user_id = user.pk

        def emit(**record):
            message = json.dumps(record, sort_keys=True, default=str)
            level = {
                'error': logging.ERROR,
                'skipped_job': logging.WARNING,
            }.get(record['event'], logging.INFO)
            logger.log(level, message)
            # Keep a complete audit on stdout even when Django's logging
            # configuration filters INFO records or routes logs elsewhere.
            self.stdout.write(message)
            self.stdout.flush()

        def behavior_label(value):
            """Render known values, including legacy constant-name strings, for audit output."""
            try:
                return CertificatesDisplayBehaviors(value).value
            except ValueError:
                # Some historical writes stored str(enum) in the String field.
                # Resolve only real enum members; never guess unknown values.
                prefix = 'CertificatesDisplayBehaviors.'
                if isinstance(value, str) and value.startswith(prefix):
                    member = CertificatesDisplayBehaviors.__members__.get(value[len(prefix):])
                    if member is not None:
                        return member.value
                return f'INVALID({value!r})'

        counts = Counter()
        changes = {}
        emit(
            event='start', timestamp=timezone.now(), mode='apply' if apply else 'preview',
            username=username, user_id=user_id, org=org,
        )
        store = modulestore()
        # Sort in Python for deterministic, case-insensitive alphabetical order
        # independent of the SQL database's collation. Group duplicate targets so
        # historical jobs cannot repeatedly overwrite the same course.
        jobs = sorted(
            CourseRerunJob.objects.select_related('batch__settings').all(),
            key=lambda job: (job.target_course_key.casefold(), job.target_course_key, job.created_at, str(job.id)),
        )
        for target, grouped in groupby(jobs, key=lambda job: job.target_course_key):
            if org is not None:
                # Parse the organization rather than matching a string prefix:
                # ORG must not include ORG2, and legacy course keys also work.
                try:
                    target_org = CourseKey.from_string(target).org
                except InvalidKeyError as exc:
                    counts['errors'] += 1
                    emit(event='error', course=target, reason='invalid_target_course_key', error=str(exc))
                    continue
                if target_org != org:
                    counts['filtered_courses'] += 1
                    continue
            candidates = []
            for job in grouped:
                source = {'job_id': str(job.id), 'batch_id': str(job.batch_id) if job.batch_id else None}
                reason = None
                settings = None
                if job.batch is None:
                    reason = 'missing_batch'
                else:
                    try:
                        settings = job.batch.settings
                    except CourseRerunSettings.DoesNotExist:
                        reason = 'missing_settings'
                    if job.batch.is_dry_run:
                        reason = 'dry_run_batch'
                    elif job.rolled_back or job.batch.rollback_requested:
                        reason = 'rollback'
                    elif not job.is_terminal:
                        reason = 'unfinished_job'
                if settings is not None:
                    source.update(settings_id=str(settings.id), cert_display=settings.cert_display)
                if reason:
                    counts['skipped_jobs'] += 1
                    emit(event='skipped_job', course=target, reason=reason, **source)
                else:
                    candidates.append(source)
            if not candidates:
                continue

            record = {'course': target, 'sources': candidates, 'field': 'certificates_display_behavior'}
            desired_values = {source['cert_display'] for source in candidates}
            if len(desired_values) != 1 or not desired_values.issubset(CourseRerunSettings.CertDisplay.values):
                counts['errors'] += 1
                emit(event='error', reason='conflicting_or_invalid_cert_display', **record)
                continue
            desired = desired_values.pop()
            record['expected'] = desired
            try:
                # Validate against the installed platform enum, not only the
                # plugin's choices (some platforms do not support early_with_info).
                desired_behavior = CertificatesDisplayBehaviors(desired)
                record['expected'] = behavior_label(desired_behavior)
                key = CourseKey.from_string(target)
                course = store.get_course(key)
                if course is None:
                    raise ValueError('Target course does not exist in modulestore.')
                record['before_raw'] = course.certificates_display_behavior
                # Match the effective behavior shown in Studio Settings & Details.
                # A literal "CertificatesDisplayBehaviors.EARLY_NO_INFO" is not
                # the valid stored value "early_no_info": Studio treats it as
                # end_with_date when an availability date exists, or end otherwise.
                # Normalizing that literal with behavior_label alone would wrongly
                # report before=early_no_info and hide the learner-visible change.
                # Keep before_raw for diagnosis and compare the raw value below
                # so malformed storage is still repaired even if labels match.
                _, effective_before = CourseDetails.validate_certificate_settings(
                    getattr(course, 'certificate_available_date', None),
                    course.certificates_display_behavior,
                )
                record['before'] = behavior_label(effective_before)
                try:
                    current_behavior = CertificatesDisplayBehaviors(course.certificates_display_behavior)
                except ValueError:
                    # Invalid stored strings also need repair from batch settings.
                    current_behavior = None
                if current_behavior == desired_behavior:
                    outcome = 'unchanged'
                    record['saved'] = behavior_label(course.certificates_display_behavior)
                elif not apply:
                    outcome = 'would_update'
                else:
                    # Log intent before saving: if the write succeeds but the
                    # verification fails, the audit still records the old value.
                    emit(event='updating', **record)
                    # The course field is a String: persist the enum's value,
                    # avoiding a literal "CertificatesDisplayBehaviors.END".
                    course.certificates_display_behavior = desired_behavior.value
                    store.update_item(course, user_id)
                    # Re-fetch instead of reporting only the assigned in-memory value.
                    saved = store.get_course(key)
                    saved_value = saved.certificates_display_behavior if saved is not None else None
                    record['saved'] = behavior_label(saved_value)
                    if saved is None or CertificatesDisplayBehaviors(saved_value) != desired_behavior:
                        raise ValueError('Post-save course value does not match batch cert_display.')
                    outcome = 'updated'
                counts[outcome] += 1
                if outcome in ('updated', 'would_update'):
                    # Preserve alphabetical order and distinguish proposed changes
                    # from changes verified by reading the course after saving.
                    changes.setdefault(outcome, []).append({
                        'course_id': target,
                        'certificates_display_behavior': {
                            'before': record['before'],
                            'after': record['saved'] if outcome == 'updated' else record['expected'],
                        },
                    })
                emit(event=outcome, **record)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Continue to audit remaining courses, then exit nonzero below.
                counts['errors'] += 1
                emit(event='error', error_type=type(exc).__name__, error=str(exc), **record)

        emit(
            event='summary', timestamp=timezone.now(), mode='apply' if apply else 'preview',
            org=org, counts=dict(counts), changes=changes,
        )
        if counts['errors']:
            raise CommandError('Some courses could not be reconciled; see the JSON-lines audit above.')
