"""
Project-wide pytest configuration.
"""
import pytest


@pytest.fixture(scope='session', autouse=True)
def configure_celery_for_tests():
    """
    Configure the Celery app for synchronous in-process execution during tests.

    task_always_eager  — apply_async runs the task inline; no broker needed.
    task_eager_propagates — False so that exceptions raised inside a nested
        apply_async call (e.g. apply_course_settings called from within
        run_course_rerun) do not bubble up through the caller's except block
        and trigger unintended retries.
    """
    from celery import current_app  # pylint: disable=import-outside-toplevel
    current_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=False,
    )
