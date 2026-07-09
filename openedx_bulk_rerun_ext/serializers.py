"""
Serializers for openedx_bulk_rerun_ext.
"""
from django.utils import timezone
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import serializers

from .models import BulkRerunBatch, CourseRerunJob, CourseRerunLog, CourseRerunSettings, CourseRerunTeamMember

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

# ── Phase 3 serializers (defined before BulkRerunBatchCreateSerializer so they
#    can be used as nested fields) ────────────────────────────────────────────


class CourseRerunSettingsSerializer(serializers.ModelSerializer):
    """Write serializer for CourseRerunSettings; nested inside BulkRerunBatchCreateSerializer."""

    class Meta:
        """Expose all operator-configurable settings fields."""

        model = CourseRerunSettings
        fields = [
            'course_start', 'course_end',
            'enrollment_start', 'enrollment_end',
            'pacing',
            'course_mode', 'cert_display', 'create_cert', 'student_gen_cert', 'cert_on_dashboard',
            'gating_mode', 'gating_min_score', 'gating_min_completion',
            'remove_provisioner_after',
        ]

    def validate(self, attrs):
        """Enforce scheduling window constraints: start < end, enrollment within course window."""
        if attrs['course_start'] >= attrs['course_end']:
            raise serializers.ValidationError('course_start must be before course_end.')
        if attrs['enrollment_start'] > attrs['course_start']:
            raise serializers.ValidationError('enrollment_start must be on or before course_start.')
        if attrs['enrollment_end'] > attrs['course_end']:
            raise serializers.ValidationError('enrollment_end must be on or before course_end.')
        return attrs


class CourseRerunTeamMemberSerializer(serializers.ModelSerializer):
    """Write serializer for a single CAR team member entry."""

    class Meta:
        """Expose the three fields submitted by the UI Team & Access tab."""

        model = CourseRerunTeamMember
        fields = ['org', 'email', 'studio_role', 'discussion_role']


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
    settings = CourseRerunSettingsSerializer()
    team_members = CourseRerunTeamMemberSerializer(many=True, default=list)

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


class CourseRerunLogSerializer(serializers.ModelSerializer):
    """Read serializer for individual log lines."""

    class Meta:
        """Expose all log line fields."""

        model = CourseRerunLog
        fields = ['id', 'level', 'message', 'created_at']


class CourseRerunJobBriefSerializer(serializers.ModelSerializer):
    """Compact job summary nested inside a batch detail response."""

    elapsed_seconds = serializers.SerializerMethodField()
    logs = CourseRerunLogSerializer(many=True, read_only=True)

    class Meta:
        """Expose timing and status fields relevant to the Track Progress UI."""

        model = CourseRerunJob
        fields = [
            'id', 'position', 'status', 'settings_applied',
            'source_course_key', 'target_course_key',
            'started_at', 'completed_at', 'elapsed_seconds',
            'error_message',
            'logs',
        ]

    def get_elapsed_seconds(self, obj):
        """Return wall-clock seconds since started_at; uses now() for in-progress jobs."""
        if obj.started_at:
            end = obj.completed_at or timezone.now()
            return (end - obj.started_at).total_seconds()
        return None


class BulkRerunBatchSerializer(serializers.ModelSerializer):
    """Read serializer for the batch detail endpoint (includes nested jobs + logs)."""

    jobs = CourseRerunJobBriefSerializer(many=True, read_only=True)
    total_jobs = serializers.IntegerField(read_only=True)
    done_jobs = serializers.IntegerField(read_only=True)
    failed_jobs = serializers.IntegerField(read_only=True)
    settings_applied_count = serializers.SerializerMethodField()
    phase = serializers.SerializerMethodField()

    class Meta:
        """Expose all batch-level fields plus the nested jobs list."""

        model = BulkRerunBatch
        fields = [
            'id', 'status', 'mode', 'is_dry_run', 'target_run', 'prog_id',
            'total_jobs', 'done_jobs', 'failed_jobs', 'settings_applied_count',
            'phase',
            'created_at', 'completed_at',
            'jobs',
        ]

    def get_settings_applied_count(self, obj):
        """Return the number of jobs in this batch that have had settings applied."""
        return obj.jobs.filter(settings_applied=True).count()

    def get_phase(self, obj):
        """
        Derive the active UI phase from batch status.

        The batch model has no explicit phase column — phases 2 and 3 (Discovery
        sync, program linking) run inside the same Celery task chain and are not
        separately tracked.  Phase 4 signals completion to the frontend so it can
        show the Export summary button and stop polling.
        """
        if obj.status in (
            BulkRerunBatch.Status.SUCCEEDED,
            BulkRerunBatch.Status.FAILED,
            BulkRerunBatch.Status.PARTIAL,
        ):
            return 4
        return 1  # pending or running — course creation phase


class CourseRerunJobStatusSerializer(CourseRerunJobBriefSerializer):
    """
    Job summary without nested logs.

    Used by the batch detail endpoint when ``?include_logs=false`` — the
    polling payload stays constant-size regardless of how many log lines the
    batch has produced.  Log lines are fetched incrementally per job via
    CourseRerunJobLogsView (?since=<id>) instead.
    """

    logs = None

    class Meta(CourseRerunJobBriefSerializer.Meta):
        """Same fields as the brief serializer minus the nested logs list."""

        fields = [f for f in CourseRerunJobBriefSerializer.Meta.fields if f != 'logs']


class BulkRerunBatchStatusSerializer(BulkRerunBatchSerializer):
    """Batch detail without nested job logs — see CourseRerunJobStatusSerializer."""

    jobs = CourseRerunJobStatusSerializer(many=True, read_only=True)


class BulkRerunBatchSummarySerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for the batch list endpoint.

    Omits nested jobs and logs (which can be large) but includes config_json
    so the frontend can reconstruct the progress UI display context (org names,
    program info, fromMode, etc.) when recovering in-flight batches after a
    page refresh or on a different device.
    """

    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        """Expose summary fields for the batch list endpoint; excludes nested jobs and logs."""

        model = BulkRerunBatch
        fields = [
            'id', 'status', 'mode', 'is_dry_run', 'target_run', 'prog_id',
            'created_at', 'completed_at', 'created_by_username',
            'config_json',
        ]
