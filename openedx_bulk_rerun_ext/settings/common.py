"""
Common plugin settings for openedx_bulk_rerun_ext.
"""


def plugin_settings(settings):  # pylint: disable=unused-argument
    """
    Called by edx-platform's plugin framework during startup for both LMS and CMS.
    DRF and Celery are already configured by edx-platform; nothing extra is needed here.
    """
