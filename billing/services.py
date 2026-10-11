import os
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from savings.paystack import PaystackError, paystack_request

from .models import CheckoutAttempt, Subscription


class BillingError(Exception):
    pass


def configured_plan():
    plan_code = os.environ.get("PAYSTACK_PRO_PLAN_CODE", "").strip()
    if not plan_code:
        raise BillingError("Paystack Pro billing is not configured on the server.")
    try:
        plan = paystack_request("GET", f"/plan/{plan_code}")
    except PaystackError as exc:
        raise BillingError(str(exc)) from exc

    if (
        plan.get("plan_code") != plan_code
        or plan.get("interval") != "monthly"
        or plan.get("currency") != "NGN"
        or not isinstance(plan.get("amount"), int)
        or plan["amount"] <= 0
    ):
        raise BillingError("The configured Paystack Pro plan must be an NGN monthly plan.")
    return plan


def _token_cipher():
    raw_key = os.environ.get("BILLING_TOKEN_ENCRYPTION_KEY", "")
    try:
        key = raw_key.encode("ascii")
        return Fernet(key)
    except (UnicodeEncodeError, ValueError) as exc:
        raise BillingError(
            "Set BILLING_TOKEN_ENCRYPTION_KEY to a valid Fernet key before enabling Pro billing."
        ) from exc


def validate_token_encryption():
    _token_cipher()


def encrypt_email_token(token):
    if not token:
        raise BillingError("Paystack did not provide the subscription management token.")
    return _token_cipher().encrypt(token.encode()).decode()


def decrypt_email_token(encrypted_token):
    try:
        return _token_cipher().decrypt(encrypted_token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise BillingError("The stored subscription credential could not be decrypted.") from exc


def _get_period_end(data):
    raw = data.get("next_payment_date")
    parsed = parse_datetime(raw) if isinstance(raw, str) else None
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _plan_code(data):
    plan = data.get("plan")
    if isinstance(plan, dict):
        return plan.get("plan_code")
    if isinstance(plan, str):
        return plan
    return data.get("plan_code")


def _subscription_code(data):
    subscription = data.get("subscription")
    if isinstance(subscription, dict):
        return subscription.get("subscription_code") or data.get("subscription_code")
    return data.get("subscription_code")


def process_subscription_event(event_type, data):
    if not isinstance(data, dict):
        return False

    if event_type == "subscription.create":
        plan_code = _plan_code(data)
        customer = data.get("customer")
        email = customer.get("email", "") if isinstance(customer, dict) else ""
        subscription_code = _subscription_code(data)
        email_token = data.get("email_token")
        amount = data.get("amount")
        if (
            plan_code != os.environ.get("PAYSTACK_PRO_PLAN_CODE")
            or not email
            or not subscription_code
            or not email_token
            or not isinstance(amount, int)
            or data.get("status") != Subscription.ACTIVE
        ):
            return False
        attempt = (
            CheckoutAttempt.objects.filter(
                user__email__iexact=email,
                plan_code=plan_code,
                status=CheckoutAttempt.PENDING,
                created_at__gte=timezone.now() - timedelta(hours=48),
            )
            .select_related("user")
            .first()
        )
        if not attempt or amount != attempt.amount_kobo:
            return False
        existing = Subscription.objects.filter(subscription_code=subscription_code).first()
        if existing:
            return existing.user_id == attempt.user_id
        subscription, _ = Subscription.objects.update_or_create(
            user=attempt.user,
            defaults={
                "plan_code": plan_code,
                "subscription_code": subscription_code,
                "encrypted_email_token": encrypt_email_token(email_token),
                "status": Subscription.ACTIVE,
                "amount_kobo": amount,
                "currency": "NGN",
                "next_payment_date": _get_period_end(data),
                "cancel_at_period_end": False,
            },
        )
        attempt.status = CheckoutAttempt.COMPLETED
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["status", "completed_at"])
        return bool(subscription.pk)

    code = _subscription_code(data)
    if not code:
        return False
    subscription = Subscription.objects.filter(subscription_code=code).first()
    if not subscription:
        return False

    if event_type == "invoice.payment_successful":
        subscription.status = Subscription.ACTIVE
        subscription.next_payment_date = _get_period_end(data) or subscription.next_payment_date
        subscription.cancel_at_period_end = False
    elif event_type == "invoice.payment_failed":
        subscription.status = Subscription.PAST_DUE
    elif event_type == "subscription.not_renew":
        subscription.cancel_at_period_end = True
        subscription.next_payment_date = _get_period_end(data) or subscription.next_payment_date
    elif event_type == "subscription.disable":
        period_end = _get_period_end(data) or subscription.next_payment_date
        if period_end and period_end > timezone.now():
            subscription.status = Subscription.ACTIVE
            subscription.cancel_at_period_end = True
            subscription.next_payment_date = period_end
        else:
            subscription.status = Subscription.CANCELLED
            subscription.cancel_at_period_end = False
    else:
        return False

    subscription.save(
        update_fields=[
            "status",
            "next_payment_date",
            "cancel_at_period_end",
            "updated_at",
        ]
    )
    return True
