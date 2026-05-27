"""
Admin registration for openedx_bulk_rerun_ext.
"""
from django.contrib import admin

from .models import BulkRerunBatch, CourseRerunJob, CourseRerunLog


class CourseRerunLogInline(admin.TabularInline):
    """Read-only log line display nested inside a CourseRerunJob change page."""

    model = CourseRerunLog
    extra = 0
    can_delete = False
    readonly_fields = ['id', 'level', 'message', 'created_at']
    ordering = ['created_at']

    def has_add_permission(self, _request, _obj=None):
        """Disallow manual log creation; logs are append-only via the task."""
        return False


class CourseRerunJobInline(admin.TabularInline):
    """Compact job summary nested inside a BulkRerunBatch change page."""

    model = CourseRerunJob
    extra = 0
    can_delete = False
    readonly_fields = [
        'id', 'position', 'status', 'job_type',
        'source_course_key', 'target_course_key',
        'started_at', 'completed_at', 'error_message',
    ]
    fields = readonly_fields
    ordering = ['position']

    def has_add_permission(self, _request, _obj=None):
        """Jobs are created via the API; disallow manual creation via admin."""
        return False


@admin.register(BulkRerunBatch)
class BulkRerunBatchAdmin(admin.ModelAdmin):
    """Admin view for monitoring and diagnosing bulk rerun submissions."""

    list_display = ['id', 'status', 'mode', 'is_dry_run', 'target_run', 'created_by', 'created_at', 'completed_at']
    list_filter = ['status', 'mode', 'is_dry_run']
    search_fields = ['target_run', 'prog_id']
    readonly_fields = ['id', 'created_at', 'completed_at', 'created_by']
    inlines = [CourseRerunJobInline]


@admin.register(CourseRerunJob)
class CourseRerunJobAdmin(admin.ModelAdmin):
    """Admin view for inspecting individual rerun jobs and their log output."""

    list_display = ['id', 'status', 'job_type', 'source_course_key', 'target_course_key', 'created_by', 'created_at']
    list_filter = ['status', 'job_type']
    search_fields = ['source_course_key', 'target_course_key']
    readonly_fields = ['id', 'created_at', 'started_at', 'completed_at', 'celery_task_id']
    inlines = [CourseRerunLogInline]
