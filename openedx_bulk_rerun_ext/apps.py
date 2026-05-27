"""
openedx_bulk_rerun_ext Django application initialization.
"""

from django.apps import AppConfig
from edx_django_utils.plugins import PluginSettings, PluginURLs


class OpenedxBulkRerunExtConfig(AppConfig):
    """
    Configuration for the openedx_bulk_rerun_ext Django application.
    """

    name = 'openedx_bulk_rerun_ext'
    default_auto_field = 'django.db.models.BigAutoField'

    plugin_app = {
        PluginURLs.CONFIG: {
            'cms.djangoapp': {
                PluginURLs.NAMESPACE: 'bulk_rerun',
                PluginURLs.REGEX: r'^api/bulk-rerun/',
                PluginURLs.RELATIVE_PATH: 'urls',
            },
        },
        PluginSettings.CONFIG: {
            'cms.djangoapp': {
                'common': {PluginSettings.RELATIVE_PATH: 'settings.common'},
                'production': {PluginSettings.RELATIVE_PATH: 'settings.production'},
            },
        },
    }
