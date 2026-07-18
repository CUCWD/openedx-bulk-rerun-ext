"""Permissions used by the bulk-rerun API."""

from rest_framework.permissions import BasePermission


class IsSuperuser(BasePermission):
    """Allow access only to authenticated Django superusers."""

    def has_permission(self, request, view):
        """Return whether the request is from an authenticated superuser."""
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)
