"""
Serializers for openedx_bulk_rerun_ext.
"""
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import serializers

from .models import BulkRerunBatch, CourseRerunJob, CourseRerunLog

# ── Phase 1 serializers ───────────────────────────────────────────────────────


class ValidateKeysSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Validates the request body for POST /api/bulk-rerun/validate/."""

    # 500 is an arbitrary ceiling that keeps the query and modulestore
    # calls from becoming unreasonably large in a single request.
    keys = serializers.ListField(
        child=serializers.CharField(),
        min_length=1,
        max_length=500,
    )


class CreateCourseRerunJobSerializer(serializers.Serializer):  # pylint: disable=abstract-method
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
        """Bind the serializer to CourseRerunJob and expose all fields."""

        model = CourseRerunJob
        fields = '__all__'


# ── Phase 2 serializers ───────────────────────────────────────────────────────

class _CourseEntrySerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Validates a single course entry within a batch create request."""

    source_course_key = serializers.CharField()
    target_course_key = serializers.CharField()
    job_type = serializers.ChoiceField(
        choices=CourseRerunJob.JobType.choices,
        default=CourseRerunJob.JobType.INDIVIDUAL,
    )


class BulkRerunBatchCreateSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Validates the request body for POST /api/bulk-rerun/batches/."""

    mode = serializers.ChoiceField(choices=BulkRerunBatch.Mode.choices)
    is_dry_run = serializers.BooleanField(default=False)
    target_run = serializers.CharField(max_length=64)
    prog_id = serializers.CharField(max_length=32, required=False, default='')
    courses = serializers.ListField(
        child=_CourseEntrySerializer(),
        min_length=1,
        max_length=200,
    )

    def validate(self, attrs):
        """Enforce key validity, source≠target, and no duplicate targets within the batch."""
        courses = attrs['courses']

        for entry in courses:
            for field in ('source_course_key', 'target_course_key'):
                try:
                    CourseKey.from_string(entry[field])
                except InvalidKeyError as exc:
                    raise serializers.ValidationError(str(exc)) from exc
            if entry['source_course_key'] == entry['target_course_key']:
                raise serializers.ValidationError(
                    f"source_course_key and target_course_key must differ: {entry['target_course_key']}"
                )

        targets = [e['target_course_key'] for e in courses]
        if len(targets) != len(set(targets)):
            raise serializers.ValidationError(
                'Duplicate target_course_key values within the batch.'
            )

        return attrs


class CourseRerunJobBriefSerializer(serializers.ModelSerializer):
    """Compact job summary nested inside a batch detail response."""

    elapsed_seconds = serializers.SerializerMethodField()

    class Meta:
        """Expose timing and status fields relevant to the Track Progress UI."""

        model = CourseRerunJob
        fields = [
            'id', 'position', 'status',
            'source_course_key', 'target_course_key',
            'started_at', 'completed_at', 'elapsed_seconds',
            'error_message',
        ]

    def get_elapsed_seconds(self, obj):
        """Return wall-clock seconds between started_at and completed_at, or None."""
        if obj.started_at and obj.completed_at:
            return (obj.completed_at - obj.started_at).total_seconds()
        return None


class BulkRerunBatchSerializer(serializers.ModelSerializer):
    """Read serializer for the batch detail and list endpoints."""

    jobs = CourseRerunJobBriefSerializer(many=True, read_only=True)
    total_jobs = serializers.IntegerField(read_only=True)
    done_jobs = serializers.IntegerField(read_only=True)
    failed_jobs = serializers.IntegerField(read_only=True)

    class Meta:
        """Expose all batch-level fields plus the nested jobs list."""

        model = BulkRerunBatch
        fields = [
            'id', 'status', 'mode', 'is_dry_run', 'target_run', 'prog_id',
            'total_jobs', 'done_jobs', 'failed_jobs',
            'created_at', 'completed_at',
            'jobs',
        ]


class CourseRerunLogSerializer(serializers.ModelSerializer):
    """Read serializer for individual log lines."""

    class Meta:
        """Expose all log line fields."""

        model = CourseRerunLog
        fields = ['id', 'level', 'message', 'created_at']
