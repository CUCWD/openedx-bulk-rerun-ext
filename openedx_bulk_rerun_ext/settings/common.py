"""
Common plugin settings for openedx_bulk_rerun_ext.
"""


def plugin_settings(settings):
    """
    Configure plugin-specific Django settings.

    Invoked by edx-platform's plugin framework during startup for both LMS and CMS.
    DRF and Celery are already configured by edx-platform; nothing extra is needed here.
    """
    # Maximum number of course rerun tasks running concurrently per batch.
    # Increase carefully — each concurrent rerun copies the full course content.
    if not hasattr(settings, 'BULK_RERUN_MAX_CONCURRENT'):
        settings.BULK_RERUN_MAX_CONCURRENT = 3
    if not hasattr(settings, 'BULK_RERUN_SETTINGS_MAX_RETRIES'):
        settings.BULK_RERUN_SETTINGS_MAX_RETRIES = 2
    if not hasattr(settings, 'BULK_RERUN_CELERY_QUEUE'):
        settings.BULK_RERUN_CELERY_QUEUE = 'edx.cms.core.bulk_rerun'
