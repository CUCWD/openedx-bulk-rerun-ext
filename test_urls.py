"""
URL configuration used only during tests.

ROOT_URLCONF in test_settings.py points here so that include() registers the
'bulk_rerun' namespace, which is required for reverse() calls in tests.
"""
from django.urls import include, path

urlpatterns = [
    path('', include('openedx_bulk_rerun_ext.urls')),
]
