"""Regression tests for historical certificate display reconciliation."""
import json
import logging
import sys
from enum import Enum
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from openedx_bulk_rerun_ext.models import CourseRerunJob, CourseRerunSettings


class CertificatesDisplayBehaviors(Enum):
    """Use a plain Enum to catch accidental comparisons against raw strings."""

    END = 'end'
    END_WITH_DATE = 'end_with_date'
    EARLY_NO_INFO = 'early_no_info'


def job(target, display='early_no_info', **overrides):
    """Build a historical job with its eagerly loaded batch/settings link."""
    values = {
        'id': target, 'batch_id': 'batch', 'target_course_key': f'course-v1:ORG+{target}+RUN',
        'created_at': 1, 'rolled_back': False, 'is_terminal': True,
        'batch': SimpleNamespace(
            is_dry_run=False, rollback_requested=False,
            settings=SimpleNamespace(id='settings', cert_display=display),
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def run_command(jobs, store, *, apply=False, org=None, username='operator', user_exists=True):
    """Run against controlled SQL rows and a platform modulestore mock."""
    output = StringIO()

    def validate_certificate_settings(available_date, behavior):
        if behavior in ('early_no_info', CertificatesDisplayBehaviors.EARLY_NO_INFO):
            return None, CertificatesDisplayBehaviors.EARLY_NO_INFO
        if available_date:
            return available_date, CertificatesDisplayBehaviors.END_WITH_DATE
        return None, CertificatesDisplayBehaviors.END

    modules = {
        'xmodule.modulestore.django': MagicMock(modulestore=MagicMock(return_value=store)),
        'xmodule.data': SimpleNamespace(CertificatesDisplayBehaviors=CertificatesDisplayBehaviors),
        'openedx.core.djangoapps.models.course_details': SimpleNamespace(
            CourseDetails=SimpleNamespace(validate_certificate_settings=validate_certificate_settings),
        ),
    }
    with patch.dict(sys.modules, modules), patch.object(CourseRerunJob, 'objects') as manager:
        manager.select_related.return_value.all.return_value = jobs
        with patch(
            'openedx_bulk_rerun_ext.management.commands.backfill_certificate_display.get_user_model',
        ) as user_model:
            user_model.return_value.objects.filter.return_value.first.return_value = (
                SimpleNamespace(pk=1) if user_exists else None
            )
            error = None
            try:
                call_command('backfill_certificate_display', apply=apply, username=username, org=org, stdout=output)
            except CommandError as exc:
                error = exc
    return [json.loads(line) for line in output.getvalue().splitlines()], error


def test_preview_orders_courses_without_writing():
    store = MagicMock()
    store.get_course.return_value = SimpleNamespace(certificates_display_behavior='end')
    records, error = run_command([job('Z'), job('a')], store)
    assert error is None
    assert [r['course'] for r in records if r['event'] == 'would_update'] == [
        'course-v1:ORG+a+RUN', 'course-v1:ORG+Z+RUN',
    ]
    store.update_item.assert_not_called()

    assert records[-1]['changes']['would_update'] == [
        {
            'course_id': f'course-v1:ORG+{target}+RUN',
            'certificates_display_behavior': {
                'before': 'end',
                'after': 'early_no_info',
            },
        }
        for target in ('a', 'Z')
    ]


@pytest.mark.parametrize('username, user_exists', [(None, True), ('missing', False)])
def test_apply_requires_existing_username(username, user_exists):
    store = MagicMock()
    records, error = run_command([job('A')], store, apply=True, username=username, user_exists=user_exists)
    assert error is not None
    assert records == []
    store.get_course.assert_not_called()
    store.update_item.assert_not_called()


def test_preview_does_not_require_username():
    store = MagicMock()
    store.get_course.return_value = SimpleNamespace(certificates_display_behavior='end')
    records, error = run_command([job('A')], store, username=None)
    assert error is None
    assert records[0]['username'] is None
    assert records[0]['user_id'] is None
    store.update_item.assert_not_called()


def test_apply_reads_back_saved_value_and_repeat_is_unchanged():
    store = MagicMock()
    persisted = {'value': 'end'}
    store.get_course.side_effect = lambda key: SimpleNamespace(certificates_display_behavior=persisted['value'])
    store.update_item.side_effect = lambda course, user: persisted.update(value=course.certificates_display_behavior)
    records, error = run_command([job('A')], store, apply=True)
    assert error is None
    updated = next(r for r in records if r['event'] == 'updated')
    assert updated['before'] == 'end'
    assert records[0]['username'] == 'operator'
    assert records[0]['user_id'] == 1
    assert store.update_item.call_args.args[1] == 1
    assert updated['saved'] == updated['expected'] == 'early_no_info'
    assert updated['sources'][0]['cert_display'] == 'early_no_info'
    assert records[-1]['changes']['updated'] == [{
        'course_id': 'course-v1:ORG+A+RUN',
        'certificates_display_behavior': {
            'before': 'end',
            'after': 'early_no_info',
        },
    }]
    records, error = run_command([job('A')], store, apply=True)
    assert error is None
    assert any(r['event'] == 'unchanged' for r in records)
    assert store.update_item.call_count == 1
    assert records[-1]['changes'] == {}


@pytest.mark.parametrize('failure', ['missing', 'save', 'verification'])
def test_errors_reported_and_remaining_courses_processed(failure):
    store = MagicMock()
    course = SimpleNamespace(certificates_display_behavior='end')
    store.get_course.side_effect = lambda key: None if failure == 'missing' else SimpleNamespace(**vars(course))
    if failure == 'save':
        store.update_item.side_effect = RuntimeError('write failed')
    records, error = run_command([job('A'), job('B')], store, apply=True)
    assert error is not None
    assert len([r for r in records if r['event'] == 'error']) == 2
    assert not any(r['event'] == 'updated' for r in records)
    assert records[-1]['changes'] == {}


def test_conflicts_and_ineligible_jobs_do_not_write():
    store = MagicMock()
    records, error = run_command([
        job('A'), job('A', display='end'), job('B', rolled_back=True), job('C', is_terminal=False),
    ], store, apply=True)
    assert error is not None
    assert records[-1]['counts'] == {'errors': 1, 'skipped_jobs': 2}
    store.update_item.assert_not_called()


@pytest.mark.parametrize('apply', [False, True])
def test_org_filter_only_accesses_exact_target_organization(apply):
    store = MagicMock()
    persisted = {'value': 'end'}
    store.get_course.side_effect = lambda key: SimpleNamespace(certificates_display_behavior=persisted['value'])
    store.update_item.side_effect = lambda course, user: persisted.update(value=course.certificates_display_behavior)
    records, error = run_command([
        job('A'),
        job('B', target_course_key='course-v1:ORG2+B+RUN'),
        job('C', target_course_key='course-v1:org+C+RUN'),
    ], store, apply=apply, org='ORG')
    assert error is None
    assert records[0]['org'] == records[-1]['org'] == 'ORG'
    assert records[-1]['counts']['filtered_courses'] == 2
    assert all(str(call.args[0]) == 'course-v1:ORG+A+RUN' for call in store.get_course.call_args_list)
    assert store.update_item.call_count == int(apply)


def test_org_filter_with_no_matches_does_not_access_modulestore_courses():
    store = MagicMock()
    records, error = run_command([job('A')], store, apply=True, org='OTHER')
    assert error is None
    assert records[-1]['counts'] == {'filtered_courses': 1}
    store.get_course.assert_not_called()
    store.update_item.assert_not_called()


def test_audit_events_use_logger_with_appropriate_levels(caplog):
    logger_name = 'openedx_bulk_rerun_ext.management.commands.backfill_certificate_display'
    store = MagicMock()
    store.get_course.return_value = None
    with caplog.at_level(logging.INFO, logger=logger_name):
        records, error = run_command([job('A', rolled_back=True), job('B')], store)
    assert error is not None
    logged = [record for record in caplog.records if record.name == logger_name]
    assert [json.loads(record.getMessage()) for record in logged] == records
    levels = {json.loads(record.getMessage())['event']: record.levelno for record in logged}
    assert levels == {
        'start': logging.INFO,
        'skipped_job': logging.WARNING,
        'error': logging.ERROR,
        'summary': logging.INFO,
    }


def test_matching_enum_does_not_write():
    store = MagicMock()
    store.get_course.return_value = SimpleNamespace(
        certificates_display_behavior=CertificatesDisplayBehaviors.EARLY_NO_INFO,
    )
    records, error = run_command([job('SIS')], store, apply=True)
    assert error is None
    assert records[-1]['counts'] == {'unchanged': 1}
    store.update_item.assert_not_called()


def test_different_enum_is_updated_and_enum_readback_verified():
    store = MagicMock()
    store.get_course.side_effect = [
        SimpleNamespace(certificates_display_behavior=CertificatesDisplayBehaviors.END),
        SimpleNamespace(certificates_display_behavior=CertificatesDisplayBehaviors.EARLY_NO_INFO),
    ]
    records, error = run_command([job('SIS')], store, apply=True)
    assert error is None
    assert records[-1]['counts'] == {'updated': 1}
    assert store.update_item.call_args.args[0].certificates_display_behavior == 'early_no_info'


def test_unsupported_platform_behavior_is_reported_without_writing():
    store = MagicMock()
    records, error = run_command([job('SIS', display='early_with_info')], store, apply=True)
    assert error is not None
    assert records[-1]['counts'] == {'errors': 1}
    store.update_item.assert_not_called()


@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('available_date, expected_before', [(None, 'end'), ('2026-12-01', 'end_with_date')])
def test_constant_name_string_is_labeled_and_still_repaired(apply, available_date, expected_before):
    store = MagicMock()
    raw = 'CertificatesDisplayBehaviors.EARLY_NO_INFO'
    persisted = {'value': raw}
    store.get_course.side_effect = lambda key: SimpleNamespace(
        certificates_display_behavior=persisted['value'], certificate_available_date=available_date,
    )
    store.update_item.side_effect = lambda course, user: persisted.update(value=course.certificates_display_behavior)
    records, error = run_command([job('A')], store, apply=apply)
    assert error is None
    change = records[-1]['changes']['updated' if apply else 'would_update'][0]
    audit = next(record for record in records if record['event'] == ('updated' if apply else 'would_update'))
    assert audit['before_raw'] == raw
    assert change['certificates_display_behavior'] == {'before': expected_before, 'after': 'early_no_info'}
    assert set(change) == {'course_id', 'certificates_display_behavior'}
    assert persisted['value'] == ('early_no_info' if apply else raw)


@pytest.mark.parametrize('org', ['', '   '])
def test_empty_org_is_rejected_before_accessing_courses(org):
    store = MagicMock()
    records, error = run_command([job('A')], store, org=org)
    assert str(error) == '--org must not be empty.'
    assert records == []
    store.get_course.assert_not_called()


def test_invalid_target_key_reports_error_and_continues_with_org_filter():
    store = MagicMock()
    store.get_course.return_value = SimpleNamespace(certificates_display_behavior='end')
    records, error = run_command([job('bad', target_course_key='!invalid'), job('A')], store, org='ORG')
    assert error is not None
    failure = next(record for record in records if record['event'] == 'error')
    assert failure['reason'] == 'invalid_target_course_key'
    assert failure['course'] == '!invalid'
    assert records[-1]['counts'] == {'errors': 1, 'would_update': 1}
    store.get_course.assert_called_once()
    store.update_item.assert_not_called()


@pytest.mark.parametrize('reason', ['missing_batch', 'missing_settings', 'dry_run_batch', 'rollback'])
def test_ineligible_job_logs_reason_without_loading_course(reason):
    store = MagicMock()
    candidate = job('A')
    if reason == 'missing_batch':
        candidate.batch = None
        candidate.batch_id = None
    elif reason == 'missing_settings':
        candidate.batch = MagicMock(is_dry_run=False, rollback_requested=False)
        type(candidate.batch).settings = PropertyMock(side_effect=CourseRerunSettings.DoesNotExist)
    elif reason == 'dry_run_batch':
        candidate.batch.is_dry_run = True
    else:
        candidate.batch.rollback_requested = True
    records, error = run_command([candidate], store, apply=True)
    assert error is None
    skipped = next(record for record in records if record['event'] == 'skipped_job')
    assert skipped['reason'] == reason
    assert skipped['job_id'] == candidate.id
    assert records[-1]['counts'] == {'skipped_jobs': 1}
    store.get_course.assert_not_called()
    store.update_item.assert_not_called()


@pytest.mark.parametrize('saved_value, expected_label', [
    ('CertificatesDisplayBehaviors.EARLY_NO_INFO', 'early_no_info'),
    ('CertificatesDisplayBehaviors.UNKNOWN', "INVALID('CertificatesDisplayBehaviors.UNKNOWN')"),
    ('unknown', "INVALID('unknown')"),
    (None, 'INVALID(None)'),
])
def test_invalid_readback_is_logged_and_never_reported_as_updated(saved_value, expected_label):
    store = MagicMock()
    store.get_course.side_effect = [
        SimpleNamespace(certificates_display_behavior='end'),
        SimpleNamespace(certificates_display_behavior=saved_value) if saved_value is not None else None,
    ]
    records, error = run_command([job('A')], store, apply=True)
    assert error is not None
    failure = next(record for record in records if record['event'] == 'error')
    assert failure['saved'] == expected_label
    assert records[-1]['counts'] == {'errors': 1}
    assert records[-1]['changes'] == {}
    assert not any(record['event'] == 'updated' for record in records)
    store.update_item.assert_called_once()
