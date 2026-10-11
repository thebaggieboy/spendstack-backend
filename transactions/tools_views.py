from datetime import timedelta

from django.db.models import Sum
from django.db import transaction as db_transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasActiveProSubscription
from .models import SpendingAlert, Transaction, TransactionRule
from .serializers import SpendingAlertSerializer, TransactionRuleSerializer
from .services import reapply_transaction_rules


def _alert_payload(alert, user):
    today = timezone.localdate()
    if alert.period == SpendingAlert.WEEKLY:
        start_date = today - timedelta(days=today.weekday())
    else:
        start_date = today.replace(day=1)

    spending = Transaction.objects.filter(
        user=user,
        is_transfer=False,
        amount__lt=0,
        date__gte=start_date,
    )
    if alert.category_name:
        spending = spending.filter(category__name__iexact=alert.category_name)
    total = spending.aggregate(total=Sum("amount"))["total"] or 0
    current_spend = abs(total)

    data = SpendingAlertSerializer(alert).data
    data["current_spend"] = current_spend
    data["is_exceeded"] = current_spend >= alert.threshold
    return data


class SpendingAlertListView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if self.request.method == "POST":
            permissions.append(HasActiveProSubscription())
        return permissions

    def get(self, request):
        alerts = SpendingAlert.objects.filter(user=request.user)
        return Response([_alert_payload(alert, request.user) for alert in alerts])

    def post(self, request):
        serializer = SpendingAlertSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        alert = serializer.save()
        return Response(
            _alert_payload(alert, request.user),
            status=status.HTTP_201_CREATED,
        )


class SpendingAlertDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if self.request.method in ("PATCH", "PUT"):
            permissions.append(HasActiveProSubscription())
        return permissions

    def patch(self, request, pk):
        alert = get_object_or_404(SpendingAlert, pk=pk, user=request.user)
        serializer = SpendingAlertSerializer(
            alert,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        alert = serializer.save()
        return Response(_alert_payload(alert, request.user))

    def delete(self, request, pk):
        alert = get_object_or_404(SpendingAlert, pk=pk, user=request.user)
        alert.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecurringBillsView(APIView):
    permission_classes = [IsAuthenticated, HasActiveProSubscription]

    def get(self, request):
        from .services import detect_recurring_bills

        transactions = (
            Transaction.objects.filter(user=request.user, amount__lt=0, is_transfer=False)
            .select_related("category")
            .order_by("date")
        )
        return Response(detect_recurring_bills(transactions))


class TransactionRuleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if self.request.method == "POST":
            permissions.append(HasActiveProSubscription())
        return permissions

    def get(self, request):
        serializer = TransactionRuleSerializer(
            TransactionRule.objects.filter(user=request.user),
            many=True,
        )
        return Response(serializer.data)

    @db_transaction.atomic
    def post(self, request):
        serializer = TransactionRuleSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        reapply_transaction_rules(request.user)
        return Response(
            TransactionRuleSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )


class TransactionRuleDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if self.request.method in ("PATCH", "PUT"):
            permissions.append(HasActiveProSubscription())
        return permissions

    @db_transaction.atomic
    def patch(self, request, pk):
        rule = get_object_or_404(TransactionRule, pk=pk, user=request.user)
        serializer = TransactionRuleSerializer(
            rule,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        reapply_transaction_rules(request.user)
        return Response(TransactionRuleSerializer(rule).data)

    @db_transaction.atomic
    def delete(self, request, pk):
        rule = get_object_or_404(TransactionRule, pk=pk, user=request.user)
        rule.delete()
        reapply_transaction_rules(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
