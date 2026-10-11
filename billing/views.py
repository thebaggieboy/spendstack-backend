import logging
import uuid
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from savings.paystack import PaystackError, paystack_request

from .models import CheckoutAttempt, Subscription
from .permissions import user_has_pro
from .services import (
    BillingError,
    configured_plan,
    decrypt_email_token,
    validate_token_encryption,
)


logger = logging.getLogger(__name__)


def _subscription_payload(user):
    subscription = Subscription.objects.filter(user=user).first()
    if not subscription:
        return {
            "plan": "free",
            "is_pro": False,
            "status": "free",
            "cancel_at_period_end": False,
            "next_payment_date": None,
        }
    return {
        "plan": "pro" if user_has_pro(user) else "free",
        "is_pro": user_has_pro(user),
        "status": subscription.status,
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "next_payment_date": (
            subscription.next_payment_date.isoformat()
            if subscription.next_payment_date
            else None
        ),
    }


class PlanView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            plan = configured_plan()
        except BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {
                "name": plan.get("name") or "Spendstack Pro",
                "amount": str((Decimal(plan["amount"]) / Decimal("100")).quantize(Decimal("0.01"))),
                "currency": plan["currency"],
                "interval": plan["interval"],
            }
        )


class SubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_subscription_payload(request.user))


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if user_has_pro(request.user):
            return Response(
                {"detail": "Your account already has Spendstack Pro."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            plan = configured_plan()
            validate_token_encryption()
        except BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        reference = f"SS-PRO-{uuid.uuid4().hex}"
        attempt = CheckoutAttempt.objects.create(
            user=request.user,
            reference=reference,
            plan_code=plan["plan_code"],
            amount_kobo=plan["amount"],
        )
        try:
            checkout = paystack_request(
                "POST",
                "/transaction/initialize",
                payload={
                    "email": request.user.email,
                    "plan": plan["plan_code"],
                    "currency": plan["currency"],
                    "reference": reference,
                    "callback_url": f"{settings.SPENDSTACK_FRONTEND_URL}/dashboard/upgrade?billing=return",
                    "metadata": {
                        "purpose": "spendstack_pro_subscription",
                        "billing_attempt_id": str(attempt.id),
                    },
                },
            )
        except PaystackError as exc:
            attempt.status = CheckoutAttempt.FAILED
            attempt.save(update_fields=["status"])
            logger.warning("Paystack Pro checkout initialization failed: %s", exc)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        authorization_url = checkout.get("authorization_url")
        if not isinstance(authorization_url, str) or not authorization_url.startswith(
            "https://checkout.paystack.com/"
        ):
            attempt.status = CheckoutAttempt.FAILED
            attempt.save(update_fields=["status"])
            logger.error("Paystack did not return a valid checkout URL.")
            return Response(
                {"detail": "Paystack did not provide a valid checkout URL."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {"authorization_url": authorization_url, "reference": reference},
            status=status.HTTP_201_CREATED,
        )


class CheckoutVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reference = request.query_params.get("reference", "")
        attempt = CheckoutAttempt.objects.filter(
            user=request.user,
            reference=reference,
        ).first()
        if not attempt:
            return Response(
                {"detail": "Checkout reference not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if attempt.status == CheckoutAttempt.COMPLETED:
            return Response({"status": "active", **_subscription_payload(request.user)})
        try:
            verified = paystack_request("GET", f"/transaction/verify/{reference}")
        except PaystackError as exc:
            logger.warning("Paystack Pro payment verification failed.")
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        customer = verified.get("customer")
        metadata = verified.get("metadata")
        if (
            verified.get("status") != "success"
            or verified.get("reference") != reference
            or verified.get("amount") != attempt.amount_kobo
            or verified.get("currency") != "NGN"
            or not isinstance(customer, dict)
            or customer.get("email", "").lower() != request.user.email.lower()
            or not isinstance(metadata, dict)
            or metadata.get("purpose") != "spendstack_pro_subscription"
            or metadata.get("billing_attempt_id") != str(attempt.id)
            or attempt.created_at < timezone.now() - timedelta(hours=48)
        ):
            return Response(
                {"detail": "Paystack has not verified the expected subscription payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "status": "payment_received",
                "is_pro": user_has_pro(request.user),
                "message": "Payment is verified. Pro access will turn on after Paystack confirms the subscription.",
            }
        )


class CancelSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = Subscription.objects.filter(
            user=request.user,
            status=Subscription.ACTIVE,
        ).first()
        if not subscription:
            return Response(
                {"detail": "There is no active subscription to cancel."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if subscription.cancel_at_period_end:
            return Response(_subscription_payload(request.user))
        try:
            paystack_request(
                "POST",
                "/subscription/disable",
                payload={
                    "code": subscription.subscription_code,
                    "token": decrypt_email_token(subscription.encrypted_email_token),
                },
            )
        except (PaystackError, BillingError) as exc:
            logger.warning("Paystack subscription cancellation failed.")
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        return Response(_subscription_payload(request.user))
