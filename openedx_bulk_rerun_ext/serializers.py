"""
Serializers for openedx_bulk_rerun_ext.
"""
from rest_framework import serializers

from .models import CourseRerunJob


class ValidateKeysSerializer(serializers.Serializer):
    """Validates the request body for POST /api/bulk-rerun/validate/."""

    # 500 is an arbitrary ceiling that keeps the query and modulestore
    # calls from becoming unreasonably large in a single request.
    keys = serializers.ListField(
        child=serializers.CharField(),
        min_length=1,
        max_length=500,
    )


class CreateCourseRerunJobSerializer(serializers.Serializer):
    """Validates the request body for POST /api/bulk-rerun/jobs/."""

    source_course_key = serializers.CharField()
    target_course_key = serializers.CharField()
    job_type = serializers.ChoiceField(
        choices=CourseRerunJob.JobType.choices,
        default=CourseRerunJob.JobType.INDIVIDUAL,
    )
    # allow_null + default=None lets callers omit this field entirely without
    # getting a validation error; the DB column is also nullable.
    bulk_job_id = serializers.UUIDField(required=False, allow_null=True, default=None)


class CourseRerunJobSerializer(serializers.ModelSerializer):
    """Read-only serializer for returning CourseRerunJob state to the client."""

    class Meta:
        model = CourseRerunJob
        fields = '__all__'
