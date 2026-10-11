from rest_framework.permissions import BasePermission

from .models import Subscription


def user_has_pro(user):
    if not user or not user.is_authenticated:
        return False
    subscription = Subscription.objects.filter(user=user).first()
    return bool(subscription and subscription.has_access())


class HasActiveProSubscription(BasePermission):
    def has_permission(self, request, view):
        if user_has_pro(request.user):
            return True
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(
            {
                "code": "upgrade_required",
                "detail": "This feature is included with Spendstack Pro. Visit /dashboard/upgrade to view plans.",
            }
        )
