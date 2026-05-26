"""
URLs for openedx_bulk_rerun_ext.
"""
from django.urls import path

from .views import CourseRerunJobDetail, CourseRerunJobListCreate, ValidateCourseKeysView

app_name = 'bulk_rerun'

urlpatterns = [
    path('validate/', ValidateCourseKeysView.as_view(), name='validate'),
    path('jobs/', CourseRerunJobListCreate.as_view(), name='jobs-list'),
    path('jobs/<uuid:job_id>/', CourseRerunJobDetail.as_view(), name='jobs-detail'),
]
