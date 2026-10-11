from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing.models import Subscription
from analysis.models import Statement
from transactions.models import Transaction

from .models import PageVisit
from .serializers import PageVisitSerializer

User = get_user_model()


class TrackPageVisitView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics_track"

    def post(self, request):
        serializer = PageVisitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        PageVisit.objects.create(
            user=request.user if request.user.is_authenticated else None,
            **serializer.validated_data,
        )
        return Response(status=status.HTTP_202_ACCEPTED)


class AdminAnalyticsPermission(permissions.BasePermission):
    message = "Administrator access is required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_staff or request.user.is_superuser)
        )


class AdminAnalyticsOverviewView(APIView):
    permission_classes = [AdminAnalyticsPermission]

    def get(self, request):
        now = timezone.now()
        today = timezone.localdate()
        start_today = timezone.make_aware(datetime.combine(today, time.min))
        start_7d = start_today - timedelta(days=6)
        start_30d = start_today - timedelta(days=29)

        recent_visits = PageVisit.objects.filter(created_at__gte=start_30d)
        daily_visits = {
            row["day"]: row["count"]
            for row in recent_visits.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
        }
        daily_signups = {
            row["day"]: row["count"]
            for row in User.objects.filter(date_joined__gte=start_30d)
            .annotate(day=TruncDate("date_joined"))
            .values("day")
            .annotate(count=Count("id"))
        }

        total_users = User.objects.count()
        active_subscribers = Subscription.objects.filter(status=Subscription.ACTIVE).count()
        daily_activity = [
            {
                "date": (start_30d.date() + timedelta(days=offset)).isoformat(),
                "visits": daily_visits.get(start_30d.date() + timedelta(days=offset), 0),
                "signups": daily_signups.get(start_30d.date() + timedelta(days=offset), 0),
            }
            for offset in range(30)
        ]

        return Response(
            {
                "users": {
                    "total": total_users,
                    "active_accounts": User.objects.filter(is_active=True).count(),
                    "active_1d": recent_visits.filter(
                        user__isnull=False, created_at__gte=now - timedelta(days=1)
                    ).values("user_id").distinct().count(),
                    "active_7d": recent_visits.filter(
                        user__isnull=False, created_at__gte=start_7d
                    ).values("user_id").distinct().count(),
                    "active_30d": recent_visits.filter(user__isnull=False)
                    .values("user_id")
                    .distinct()
                    .count(),
                    "signups_today": User.objects.filter(date_joined__gte=start_today).count(),
                    "signups_7d": User.objects.filter(date_joined__gte=start_7d).count(),
                    "signups_30d": User.objects.filter(date_joined__gte=start_30d).count(),
                },
                "activity": {
                    "page_views_total": PageVisit.objects.count(),
                    "page_views_30d": recent_visits.count(),
                    "visitors_30d": recent_visits.values("visitor_id").distinct().count(),
                    "top_pages": list(
                        recent_visits.values("path")
                        .annotate(views=Count("id"))
                        .order_by("-views", "path")[:8]
                    ),
                    "daily": daily_activity,
                },
                "product": {
                    "statements": Statement.objects.count(),
                    "transactions": Transaction.objects.count(),
                    "active_pro_subscribers": active_subscribers,
                    "pro_conversion_percent": round(
                        active_subscribers / total_users * 100, 1
                    )
                    if total_users
                    else 0,
                },
            }
        )


class AdminAnalyticsUsersView(APIView):
    permission_classes = [AdminAnalyticsPermission]

    def get(self, request):
        try:
            page_number = max(int(request.query_params.get("page", "1")), 1)
        except ValueError as exc:
            raise NotFound("Invalid page number.") from exc

        search = request.query_params.get("search", "").strip()[:120]
        users = User.objects.annotate(
            statement_count=Count("statements", distinct=True),
            transaction_count=Count("transactions", distinct=True),
            last_activity=Max("page_visits__created_at"),
        ).order_by("-date_joined")
        if search:
            users = users.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )

        paginator = Paginator(users, 25)
        try:
            page = paginator.page(page_number)
        except EmptyPage:
            page = paginator.page(paginator.num_pages or 1)

        return Response(
            {
                "count": paginator.count,
                "page": page.number,
                "pages": paginator.num_pages,
                "results": [
                    {
                        "id": user.id,
                        "email": user.email,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "date_joined": user.date_joined,
                        "last_login": user.last_login,
                        "last_activity": user.last_activity,
                        "is_active": user.is_active,
                        "is_staff": user.is_staff,
                        "is_superuser": user.is_superuser,
                        "statements": user.statement_count,
                        "transactions": user.transaction_count,
                    }
                    for user in page.object_list
                ],
            }
        )
