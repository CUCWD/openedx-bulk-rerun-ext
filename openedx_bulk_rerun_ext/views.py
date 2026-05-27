"""
Views for openedx_bulk_rerun_ext.
"""
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CourseRerunJob
from .serializers import CourseRerunJobSerializer, CreateCourseRerunJobSerializer, ValidateKeysSerializer

# Jobs in these statuses own the target_course_key; a new job cannot claim
# the same target until an existing one has failed (and thus released the slot).
_ACTIVE_STATUSES = [
    CourseRerunJob.Status.PENDING,
    CourseRerunJob.Status.RUNNING,
    CourseRerunJob.Status.SUCCEEDED,
]


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
