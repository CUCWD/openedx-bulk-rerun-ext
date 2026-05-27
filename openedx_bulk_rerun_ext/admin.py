"""
Admin registration for openedx_bulk_rerun_ext.
"""
from django.contrib import admin

from .models import CourseRerunJob


@admin.register(CourseRerunJob)
class CourseRerunJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'status', 'job_type', 'source_course_key', 'target_course_key', 'created_by', 'created_at']
    list_filter = ['status', 'job_type']
    search_fields = ['source_course_key', 'target_course_key']
    readonly_fields = ['id', 'created_at', 'started_at', 'completed_at', 'celery_task_id']
