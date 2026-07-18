"""
URLs for openedx_bulk_rerun_ext.
"""
from django.urls import path

from .views import (
    BulkRerunAccessView,
    BulkRerunBatchCancelView,
    BulkRerunBatchDetailView,
    BulkRerunBatchListCreateView,
    CourseRerunJobDetail,
    CourseRerunJobListCreate,
    CourseRerunJobLogsView,
    ValidateCourseKeysView,
)

app_name = 'bulk_rerun'

urlpatterns = [
    path('access/', BulkRerunAccessView.as_view(), name='access'),
    # Phase 1
    path('validate/', ValidateCourseKeysView.as_view(), name='validate'),
    path('jobs/', CourseRerunJobListCreate.as_view(), name='jobs-list'),
    path('jobs/<uuid:job_id>/', CourseRerunJobDetail.as_view(), name='jobs-detail'),
    # Phase 2
    path('batches/', BulkRerunBatchListCreateView.as_view(), name='batches-list'),
    path('batches/<uuid:batch_id>/', BulkRerunBatchDetailView.as_view(), name='batches-detail'),
    path('batches/<uuid:batch_id>/cancel/', BulkRerunBatchCancelView.as_view(), name='batches-cancel'),
    path('jobs/<uuid:job_id>/logs/', CourseRerunJobLogsView.as_view(), name='jobs-logs'),
]
