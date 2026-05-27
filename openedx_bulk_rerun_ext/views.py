"""
Views for openedx_bulk_rerun_ext.
"""
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BulkRerunBatch, CourseRerunJob
from .serializers import (
    BulkRerunBatchCreateSerializer,
    BulkRerunBatchSerializer,
    CourseRerunJobSerializer,
    CourseRerunLogSerializer,
    CreateCourseRerunJobSerializer,
    ValidateKeysSerializer,
)

# Jobs in these statuses own the target_course_key; a new job cannot claim
# the same target until an existing one has failed (and thus released the slot).
_ACTIVE_STATUSES = [
    CourseRerunJob.Status.PENDING,
    CourseRerunJob.Status.RUNNING,
    CourseRerunJob.Status.SUCCEEDED,
]


# ── Phase 1 views ─────────────────────────────────────────────────────────────


class ValidateCourseKeysView(APIView):
    """
    Check a list of target course keys for conflicts before bulk submission.

    POST /api/bulk-rerun/validate/ — accepts a list of target course keys and
    returns the subset that already exist, checking both active CourseRerunJob
    rows and the platform modulestore.  Used by the UI on each debounce cycle
    to surface conflicts before submission.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Validate a list of target course keys and return those that already exist."""
        # Deserialize and enforce min=1 / max=500 list constraints.
        serializer = ValidateKeysSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        keys = serializer.validated_data['keys']

        # Reject the entire request early if any key is malformed.
        # CourseKey.from_string() is the authoritative parser — never use regex
        # against course key strings directly.
        for key_str in keys:
            try:
                CourseKey.from_string(key_str)
            except InvalidKeyError:
                return Response(
                    {'error': f'Invalid course key: {key_str}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Pass 1: check our own DB for jobs that are pending, running, or
        # already succeeded for any of the requested target keys.
        # A succeeded job means the course already exists as a result of a
        # previous rerun, so the key is considered "existing" here too.
        existing = set(
            CourseRerunJob.objects.filter(
                target_course_key__in=keys,
                status__in=_ACTIVE_STATUSES,
            ).values_list('target_course_key', flat=True)
        )

        # Pass 2: check the platform modulestore for courses that exist
        # independently of our job table (e.g. manually created courses or
        # reruns initiated through the normal Studio UI).
        # xmodule is unavailable in test environments that run without a
        # full edx-platform install; skip the modulestore check gracefully.
        try:
            from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel,import-error
            store = modulestore()
            for key_str in keys:
                # Skip keys already found in Pass 1 to avoid redundant lookups.
                if key_str not in existing:
                    if store.has_course(CourseKey.from_string(key_str)):
                        existing.add(key_str)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # Return only the subset of input keys that already exist.
        # The UI uses this list to highlight conflicts before the user submits.
        return Response({'existing': list(existing)})


class CourseRerunJobListCreate(APIView):
    """
    List and create individual rerun jobs.

    GET /api/bulk-rerun/jobs/ — list jobs owned by the current user.

    POST /api/bulk-rerun/jobs/ — create a single rerun job and dispatch
    the Celery task immediately.

    Accepts an optional ``?bulk_job_id=<uuid>`` query param on GET to filter
    jobs belonging to the same bulk submission.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Return all rerun jobs owned by the requesting user, newest first."""
        # Scope to the requesting user so staff cannot see each other's jobs.
        jobs = CourseRerunJob.objects.filter(created_by=request.user)

        # Optional filter: return only jobs belonging to a single bulk submission.
        bulk_job_id = request.query_params.get('bulk_job_id')
        if bulk_job_id:
            jobs = jobs.filter(bulk_job_id=bulk_job_id)

        return Response(CourseRerunJobSerializer(jobs, many=True).data)

    def post(self, request):
        """Create a rerun job and immediately dispatch the Celery task."""
        # Deserialize input fields; job_type defaults to "individual" and
        # bulk_job_id defaults to None when not provided.
        serializer = CreateCourseRerunJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source_key_str = data['source_course_key']
        target_key_str = data['target_course_key']

        # Validate that both keys are parseable before touching the DB or
        # modulestore. InvalidKeyError means the string is not a valid
        # course key format (e.g. missing org, course, or run segments).
        try:
            CourseKey.from_string(source_key_str)
            CourseKey.from_string(target_key_str)
        except InvalidKeyError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # A rerun that points at itself would silently overwrite the source course.
        if source_key_str == target_key_str:
            return Response(
                {'error': 'source_course_key and target_course_key must differ'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Confirm the source course actually exists in the modulestore before
        # creating a job. Without this check the Celery task would fail with
        # an ItemNotFoundError deep inside clone_course, which is harder to debug.
        # xmodule is unavailable outside the platform; skip gracefully if so.
        try:
            from xmodule.modulestore.django import modulestore  # pylint: disable=import-outside-toplevel,import-error
            if not modulestore().has_course(CourseKey.from_string(source_key_str)):
                return Response(
                    {'error': f'Source course does not exist: {source_key_str}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # For existing-org job types, verify the target org is registered on the
        # platform before creating the job. new_org skips this check because org
        # registration (Phase 0) is a prerequisite that happens outside this flow —
        # the org won't exist yet by design.
        if data['job_type'] != CourseRerunJob.JobType.NEW_ORG:
            try:
                # pylint: disable=import-outside-toplevel,import-error
                from organizations.api import get_organization_by_short_name
                from organizations.exceptions import InvalidOrganizationException
                target_org = CourseKey.from_string(target_key_str).org
                try:
                    get_organization_by_short_name(target_org)
                except InvalidOrganizationException:
                    return Response(
                        {
                            'error': (
                                f'Organization "{target_org}" is not registered on this platform. '
                                'Register it via organizations.api.add_organization() before running a rerun.'
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        # Prevent duplicate jobs for the same target. A failed job releases the
        # slot (it is not in _ACTIVE_STATUSES) so a retry submission is allowed.
        if CourseRerunJob.objects.filter(
            target_course_key=target_key_str,
            status__in=_ACTIVE_STATUSES,
        ).exists():
            return Response(
                {'error': 'An active job for this target_course_key already exists'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # All validation passed — create the DB row in "pending" status.
        job = CourseRerunJob.objects.create(
            source_course_key=source_key_str,
            target_course_key=target_key_str,
            job_type=data['job_type'],
            bulk_job_id=data['bulk_job_id'],
            created_by=request.user,
        )

        # Dispatch the Celery task and store its ID so operators can trace it
        # in Flower or the Django admin. The task transitions the job through
        # running → succeeded/failed asynchronously.
        from .tasks import run_course_rerun  # pylint: disable=import-outside-toplevel
        result = run_course_rerun.delay(str(job.id))
        job.celery_task_id = result.id
        job.save(update_fields=['celery_task_id'])

        # Return 201 with the full job record so the client can begin polling
        # GET /api/bulk-rerun/jobs/{id}/ for status updates.
        return Response(CourseRerunJobSerializer(job).data, status=status.HTTP_201_CREATED)


class CourseRerunJobDetail(APIView):
    """
    Return the current state of a single CourseRerunJob.

    GET /api/bulk-rerun/jobs/<uuid:job_id>/ — returns 404 if the job does not
    exist or was not created by the requesting user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        """Return the current state of a single rerun job; 404 if not owned by the caller."""
        try:
            job = CourseRerunJob.objects.get(id=job_id)
        except CourseRerunJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        # Return 404 rather than 403 to avoid leaking that the job exists
        # to a user who did not create it.
        if job.created_by != request.user:
            return Response(status=status.HTTP_404_NOT_FOUND)

        return Response(CourseRerunJobSerializer(job).data)


# ── Phase 2 views ─────────────────────────────────────────────────────────────

class BulkRerunBatchListCreateView(APIView):
    """
    Submit a full bulk rerun batch from the UI.

    POST /api/bulk-rerun/batches/ — creates one BulkRerunBatch and N
    CourseRerunJob rows, then dispatches the fan-out Celery task.
    Returns 202 Accepted with the batch ID and initial job list.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Validate the batch payload, create all rows, and dispatch the fan-out task."""
        serializer = BulkRerunBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Reject any target key that already has an active or succeeded job.
        target_keys = [c['target_course_key'] for c in data['courses']]
        blocked = set(
            CourseRerunJob.objects.filter(
                target_course_key__in=target_keys,
                status__in=_ACTIVE_STATUSES,
            ).values_list('target_course_key', flat=True)
        )
        if blocked:
            return Response(
                {'error': 'Active jobs already exist for these target keys', 'keys': sorted(blocked)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = BulkRerunBatch.objects.create(
            created_by=request.user,
            mode=data['mode'],
            is_dry_run=data['is_dry_run'],
            target_run=data['target_run'],
            prog_id=data.get('prog_id', ''),
        )

        jobs = []
        for position, course in enumerate(data['courses']):
            job = CourseRerunJob.objects.create(
                batch=batch,
                position=position,
                source_course_key=course['source_course_key'],
                target_course_key=course['target_course_key'],
                job_type=course['job_type'],
                created_by=request.user,
            )
            jobs.append(job)

        from .tasks import dispatch_batch_rerun  # pylint: disable=import-outside-toplevel
        dispatch_batch_rerun.delay(str(batch.id))

        return Response(
            {
                'batch_id':   str(batch.id),
                'status':     batch.status,
                'total_jobs': len(jobs),
                'jobs': [
                    {
                        'id':                str(j.id),
                        'target_course_key': j.target_course_key,
                        'status':            j.status,
                    }
                    for j in jobs
                ],
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BulkRerunBatchDetailView(APIView):
    """
    Return the full status of a batch including per-course job state.

    GET /api/bulk-rerun/batches/<uuid:batch_id>/ — polled every 2 seconds by
    the UI Track Progress screen.  Returns 404 if the batch does not exist or
    was not created by the requesting user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        """Return batch status and the nested job list; 404 if not owned by the caller."""
        try:
            batch = BulkRerunBatch.objects.get(id=batch_id)
        except BulkRerunBatch.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if batch.created_by != request.user:
            return Response(status=status.HTTP_404_NOT_FOUND)

        return Response(BulkRerunBatchSerializer(batch).data)


class CourseRerunJobLogsView(APIView):
    """
    Return structured log lines for a single CourseRerunJob.

    GET /api/bulk-rerun/jobs/<uuid:job_id>/logs/ — returns all log lines.
    Supports ``?since=<id>`` for incremental polling; only lines with
    id > since are returned, avoiding re-fetching the full history.
    Returns 404 if the job does not exist or was not created by the caller.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        """Return log lines for a job, optionally filtered to those newer than ?since=<id>."""
        try:
            job = CourseRerunJob.objects.get(id=job_id)
        except CourseRerunJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if job.created_by != request.user:
            return Response(status=status.HTTP_404_NOT_FOUND)

        logs = job.logs.all()
        since = request.query_params.get('since')
        if since:
            try:
                logs = logs.filter(id__gt=int(since))
            except (TypeError, ValueError):
                return Response(
                    {'error': 'since must be an integer log line ID'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response({
            'job_id':     str(job.id),
            'job_status': job.status,
            'logs':       CourseRerunLogSerializer(logs, many=True).data,
        })
