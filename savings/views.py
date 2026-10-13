import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from transactions.models import Transaction
from billing.services import BillingError, process_subscription_event
from .bachs import BachsError, create_checkout_session, verify_webhook_signature
from .models import BankAccount, Budget, LockPeriod, Schedule, Wallet, WalletTransaction
from .paystack import PaystackError, paystack_request, paystack_secret_key
from .services import (
    credit_verified_funding,
    credit_bachs_funding,
    finalize_payout_attempt,
    initiate_schedule_payout,
    to_kobo,
)


logger = logging.getLogger(__name__)
MINIMUM_TOP_UP = Decimal("100.00")


def _wallet_for(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def _money(value):
    return str(value.quantize(Decimal("0.01")))


def _lock_data(lock_period):
    schedule = lock_period.payout_schedule
    return {
        "id": str(lock_period.id),
        "amount": _money(lock_period.amount),
        "lock_date": lock_period.lock_date.isoformat(),
        "unlock_date": lock_period.unlock_date.isoformat(),
        "interval_days": lock_period.interval_days,
        "status": lock_period.status,
        "description": lock_period.description,
        "bank_account": {
            "id": str(lock_period.bank_account_id),
            "bank_name": lock_period.bank_account.bank_name,
            "account_name": lock_period.bank_account.account_name,
            "account_last4": lock_period.bank_account.account_last4,
        },
        "schedule_id": str(schedule.id),
    }


def _schedule_data(schedule):
    return {
        "id": str(schedule.id),
        "lock_id": str(schedule.lock_period_id),
        "title": schedule.title,
        "amount": _money(schedule.amount),
        "payout_amount": _money(schedule.payout_amount),
        "scheduled_date": schedule.scheduled_date.isoformat(),
        "status": schedule.status,
        "auto_payout": schedule.auto_payout,
        "bank_name": schedule.bank_account.bank_name,
        "account_name": schedule.bank_account.account_name,
        "account_last4": schedule.bank_account.account_last4,
        "reference": schedule.reference,
    }


class WalletView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = _wallet_for(request.user)
        recent = WalletTransaction.objects.filter(user=request.user)[:20]
        return Response(
            {
                "currency": "NGN",
                "available_balance": _money(wallet.available_balance),
                "locked_balance": _money(wallet.locked_balance),
                "total_balance": _money(wallet.total_balance),
                "transactions": [
                    {
                        "id": str(item.id),
                        "type": item.type,
                        "amount": _money(item.amount),
                        "status": item.status,
                        "description": item.description,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in recent
                ],
            }
        )


class FundingInitializeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            amount = Decimal(str(request.data.get("amount", ""))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return Response({"detail": "Enter a valid funding amount."}, status=status.HTTP_400_BAD_REQUEST)
        if amount < MINIMUM_TOP_UP or amount > Decimal("10000000.00"):
            return Response(
                {"detail": "Funding must be at least ₦100 and no more than ₦10,000,000 per transaction."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        gateway = request.data.get("gateway", "paystack")
        if gateway not in ("paystack", "bachs"):
            return Response(
                {"detail": "Choose Paystack or Bachs as the payment gateway."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _wallet_for(request.user)
        reference = f"SS-FUND-{uuid.uuid4().hex}"
        wallet_transaction = WalletTransaction.objects.create(
            user=request.user,
            type=WalletTransaction.DEPOSIT,
            amount=amount,
            balance_before=Decimal("0.00"),
            balance_after=Decimal("0.00"),
            status=WalletTransaction.PENDING,
            reference=reference,
            description="Wallet funding",
            metadata={"payment_provider": gateway} if gateway == "bachs" else None,
        )
        if gateway == "bachs":
            success_url = (
                f"{settings.SPENDSTACK_FRONTEND_URL}/dashboard/lock-money"
                f"?funding=bachs-return&reference={reference}"
            )
            cancel_url = (
                f"{settings.SPENDSTACK_FRONTEND_URL}/dashboard/lock-money"
                "?funding=bachs-cancel"
            )
            try:
                checkout = create_checkout_session(
                    amount=_money(amount),
                    email=request.user.email,
                    reference=reference,
                    success_url=success_url,
                    cancel_url=cancel_url,
                    metadata={
                        "purpose": "spendstack_wallet_funding",
                        "wallet_transaction_id": str(wallet_transaction.id),
                    },
                )
            except BachsError as exc:
                if not exc.uncertain:
                    WalletTransaction.objects.filter(reference=reference).update(status=WalletTransaction.FAILED)
                logger.warning("Bachs wallet funding initialization failed: %s", exc)
                return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
            with transaction.atomic():
                wallet_transaction = WalletTransaction.objects.select_for_update().get(
                    pk=wallet_transaction.pk,
                )
                wallet_transaction.metadata = {
                    **(wallet_transaction.metadata or {}),
                    "payment_provider": "bachs",
                    "checkout_id": checkout["checkout_id"],
                }
                wallet_transaction.save(update_fields=["metadata"])
            return Response(
                {
                    "authorization_url": checkout["checkout_url"],
                    "reference": reference,
                    "gateway": gateway,
                },
                status=status.HTTP_201_CREATED,
            )

        callback_url = f"{settings.SPENDSTACK_FRONTEND_URL}/dashboard/lock-money?funding=return"
        try:
            data = paystack_request(
                "POST",
                "/transaction/initialize",
                payload={
                    "email": request.user.email,
                    "amount": to_kobo(amount),
                    "currency": "NGN",
                    "reference": reference,
                    "callback_url": callback_url,
                    "metadata": {
                        "purpose": "spendstack_wallet_funding",
                        "wallet_transaction_id": str(
                            WalletTransaction.objects.get(reference=reference).id
                        ),
                    },
                },
            )
        except PaystackError as exc:
            WalletTransaction.objects.filter(reference=reference).update(status=WalletTransaction.FAILED)
            logger.warning("Paystack funding initialization failed: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        authorization_url = data.get("authorization_url")
        if not isinstance(authorization_url, str) or not authorization_url.startswith(
            "https://checkout.paystack.com/"
        ):
            WalletTransaction.objects.filter(reference=reference).update(status=WalletTransaction.FAILED)
            logger.error("Paystack did not return a valid wallet funding checkout URL.")
            return Response(
                {"detail": "Paystack did not provide a valid checkout link. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {"authorization_url": authorization_url, "reference": reference, "gateway": gateway},
            status=status.HTTP_201_CREATED,
        )


class BachsFundingStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reference = request.query_params.get("reference", "")
        wallet_transaction = get_object_or_404(
            WalletTransaction,
            user=request.user,
            reference=reference,
            type=WalletTransaction.DEPOSIT,
        )
        if (wallet_transaction.metadata or {}).get("payment_provider") != "bachs":
            return Response(
                {"detail": "This payment does not use Bachs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "status": wallet_transaction.status,
                "amount": _money(wallet_transaction.amount),
            }
        )


class FundingVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reference = request.query_params.get("reference", "")
        wallet_transaction = get_object_or_404(
            WalletTransaction,
            user=request.user,
            reference=reference,
            type=WalletTransaction.DEPOSIT,
        )
        try:
            verified = paystack_request("GET", f"/transaction/verify/{reference}")
        except PaystackError as exc:
            logger.warning("Paystack funding verification failed for %s", reference)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        credited = credit_verified_funding(reference, verified)
        wallet_transaction.refresh_from_db()
        if not credited:
            return Response(
                {"detail": "Paystack could not verify a successful matching payment.", "status": wallet_transaction.status},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "status": wallet_transaction.status,
                "amount": _money(wallet_transaction.amount),
                "message": "Funding was verified and added to your available balance.",
            }
        )


class PaystackWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        secret_key = paystack_secret_key()
        signature = request.headers.get("x-paystack-signature", "")
        expected = hmac.new(secret_key.encode(), request.body, hashlib.sha512).hexdigest()
        if not secret_key or not hmac.compare_digest(signature, expected):
            return HttpResponse(status=401)
        try:
            event = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponse(status=400)

        event_type = event.get("event")
        data = event.get("data") or {}
        if event_type == "charge.success":
            reference = data.get("reference")
            if reference and WalletTransaction.objects.filter(
                reference=reference,
                type=WalletTransaction.DEPOSIT,
            ).exists():
                try:
                    verified = paystack_request("GET", f"/transaction/verify/{reference}")
                    credit_verified_funding(reference, verified)
                except PaystackError:
                    logger.exception("Could not verify Paystack funding webhook.")
                    return HttpResponse(status=503)
        elif event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
            reference = data.get("reference")
            if reference:
                finalize_payout_attempt(reference, event_type, data)
        elif event_type in {
            "subscription.create",
            "subscription.disable",
            "subscription.not_renew",
            "invoice.payment_successful",
            "invoice.payment_failed",
        }:
            try:
                process_subscription_event(event_type, data)
            except (BillingError, DatabaseError, TypeError, ValueError, KeyError):
                logger.exception("Could not process signed Paystack subscription event.")
                return HttpResponse(status=503)
        return HttpResponse(status=200)


class BachsWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        secret = os.environ.get("BACHS_WEBHOOK_SECRET", "").strip()
        signature = request.headers.get("x-bachs-signature-v2", "")
        if not verify_webhook_signature(signature, request.body, secret):
            return HttpResponse(status=401)
        try:
            event = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponse(status=400)
        if not isinstance(event, dict):
            return HttpResponse(status=400)

        if event.get("type") == "collection.succeeded":
            data = event.get("data")
            if not isinstance(data, dict):
                return HttpResponse(status=400)
            reference = data.get("reference")
            if not reference:
                metadata = data.get("metadata")
                transaction_id = metadata.get("wallet_transaction_id") if isinstance(metadata, dict) else None
                if transaction_id:
                    try:
                        transaction_id = uuid.UUID(str(transaction_id))
                    except (TypeError, ValueError, AttributeError):
                        transaction_id = None
                    if transaction_id:
                        wallet_transaction = WalletTransaction.objects.filter(
                            pk=transaction_id,
                            type=WalletTransaction.DEPOSIT,
                        ).first()
                        reference = wallet_transaction.reference if wallet_transaction else None
            if reference:
                try:
                    credited = credit_bachs_funding(reference, data)
                except DatabaseError:
                    logger.exception("Could not process signed Bachs funding webhook.")
                    return HttpResponse(status=503)
                if not credited:
                    logger.warning("Signed Bachs collection did not match a pending wallet funding transaction.")
        return HttpResponse(status=200)


class BankDirectoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            page_size = 100
            max_pages = 20
            banks_by_code = {}
            for page in range(1, max_pages + 1):
                banks = paystack_request(
                    "GET",
                    "/bank",
                    params={
                        "country": "nigeria",
                        "perPage": page_size,
                        "page": page,
                        "use_cursor": "false",
                    },
                )
                if not isinstance(banks, list):
                    raise PaystackError("Paystack returned an invalid Nigerian bank directory.")
                for bank in banks:
                    if not isinstance(bank, dict):
                        continue
                    name, code = bank.get("name"), bank.get("code")
                    if isinstance(name, str) and name and isinstance(code, str) and code:
                        banks_by_code.setdefault(code, {"name": name, "code": code})
                if len(banks) < page_size:
                    break
            else:
                raise PaystackError("Paystack bank directory pagination did not complete.")
        except PaystackError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(list(banks_by_code.values()))


class BankAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            [
                {
                    "id": str(item.id),
                    "bank_name": item.bank_name,
                    "account_name": item.account_name,
                    "account_last4": item.account_last4,
                    "is_default": item.is_default,
                }
                for item in BankAccount.objects.filter(user=request.user)
            ]
        )

    def post(self, request):
        account_number = str(request.data.get("account_number", "")).strip()
        bank_code = str(request.data.get("bank_code", "")).strip()
        bank_name = str(request.data.get("bank_name", "")).strip()
        if not account_number.isdigit() or len(account_number) != 10 or not bank_code or not bank_name:
            return Response(
                {"detail": "Enter a valid 10-digit account number and select a bank."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            resolved = paystack_request(
                "GET",
                "/bank/resolve",
                params={"account_number": account_number, "bank_code": bank_code},
            )
            recipient = paystack_request(
                "POST",
                "/transferrecipient",
                payload={
                    "type": "nuban",
                    "name": resolved.get("account_name", ""),
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "currency": "NGN",
                },
            )
        except PaystackError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        recipient_code = recipient.get("recipient_code")
        if not recipient_code or not resolved.get("account_name"):
            return Response(
                {"detail": "Paystack did not verify the bank account."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        account = BankAccount.objects.create(
            user=request.user,
            bank_name=bank_name[:120],
            bank_code=bank_code[:20],
            account_name=resolved["account_name"][:160],
            account_last4=account_number[-4:],
            recipient_code=recipient_code,
            is_default=not BankAccount.objects.filter(user=request.user).exists(),
        )
        return Response(
            {
                "id": str(account.id),
                "bank_name": account.bank_name,
                "account_name": account.account_name,
                "account_last4": account.account_last4,
                "is_default": account.is_default,
            },
            status=status.HTTP_201_CREATED,
        )


class LockPeriodListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        locks = LockPeriod.objects.filter(user=request.user).select_related("bank_account", "payout_schedule")
        return Response([_lock_data(item) for item in locks])

    def post(self, request):
        try:
            amount = Decimal(str(request.data.get("amount", ""))).quantize(Decimal("0.01"))
            interval_days = int(request.data.get("interval_days", ""))
        except (InvalidOperation, TypeError, ValueError):
            return Response(
                {"detail": "Enter a valid amount and lock duration."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if amount <= 0 or amount > Decimal("10000000.00"):
            return Response({"detail": "Lock amount must be between ₦0.01 and ₦10,000,000."}, status=status.HTTP_400_BAD_REQUEST)
        if interval_days < 1 or interval_days > 3650:
            return Response({"detail": "Lock duration must be between 1 and 3,650 days."}, status=status.HTTP_400_BAD_REQUEST)
        bank_account = get_object_or_404(
            BankAccount,
            user=request.user,
            pk=request.data.get("bank_account_id"),
        )
        auto_payout = request.data.get("auto_payout", True)
        if not isinstance(auto_payout, bool):
            return Response(
                {"detail": "auto_payout must be true or false."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        now = timezone.now()
        unlock_date = now + timedelta(days=interval_days)
        description = str(request.data.get("description", "")).strip()[:255]

        try:
            with transaction.atomic():
                wallet, _ = Wallet.objects.get_or_create(user=request.user)
                wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
                if wallet.available_balance < amount:
                    return Response(
                        {"detail": "Your available balance is too low. Add funds before creating this lock."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                available_before = wallet.available_balance
                wallet.available_balance -= amount
                wallet.locked_balance += amount
                wallet.save(update_fields=["available_balance", "locked_balance", "updated_at"])
                lock_period = LockPeriod.objects.create(
                    user=request.user,
                    amount=amount,
                    lock_date=now,
                    unlock_date=unlock_date,
                    interval_days=interval_days,
                    status=LockPeriod.LOCKED,
                    description=description,
                    bank_account=bank_account,
                )
                schedule = Schedule.objects.create(
                    user=request.user,
                    lock_period=lock_period,
                    bank_account=bank_account,
                    title=description or "Locked funds payout",
                    amount=amount,
                    payout_amount=amount,
                    scheduled_date=unlock_date,
                    status=Schedule.PENDING,
                    auto_payout=auto_payout,
                )
                WalletTransaction.objects.create(
                    user=request.user,
                    type=WalletTransaction.LOCK,
                    amount=amount,
                    balance_before=available_before,
                    balance_after=wallet.available_balance,
                    status=WalletTransaction.COMPLETED,
                    reference=f"lock-{lock_period.id}",
                    description=f"Locked for {interval_days} days",
                    metadata={
                        "lock_id": str(lock_period.id),
                        "schedule_id": str(schedule.id),
                        "locked_balance_after": _money(wallet.locked_balance),
                    },
                    completed_at=now,
                )
        except IntegrityError:
            return Response({"detail": "Could not create the lock; please try again."}, status=status.HTTP_409_CONFLICT)
        return Response(_lock_data(lock_period), status=status.HTTP_201_CREATED)


class ScheduleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        schedules = Schedule.objects.filter(user=request.user).select_related("bank_account")
        return Response([_schedule_data(item) for item in schedules])


class SchedulePayoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        schedule = get_object_or_404(Schedule, user=request.user, pk=pk)
        if schedule.scheduled_date > timezone.now():
            return Response(
                {"detail": "Funds remain locked until the scheduled unlock date."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            schedule, payout = initiate_schedule_payout(schedule.id)
        except PaystackError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except Schedule.DoesNotExist:
            return Response({"detail": "Scheduled payout was not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "schedule": _schedule_data(schedule),
                "provider_status": payout.get("status"),
                "message": "Payout submitted. Final status will update after Paystack confirms it.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BudgetListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        start_date = today.replace(day=1)
        budgets = Budget.objects.filter(user=request.user, is_active=True)
        data = []
        for budget in budgets:
            spending = Transaction.objects.filter(
                user=request.user,
                is_transfer=False,
                amount__lt=0,
                date__gte=start_date,
            )
            if budget.category_name:
                spending = spending.filter(category__name__iexact=budget.category_name)
            spent = abs(spending.aggregate(total=Sum("amount"))["total"] or Decimal("0"))
            data.append(
                {
                    "id": budget.id,
                    "name": budget.name,
                    "category_name": budget.category_name,
                    "amount": _money(budget.amount),
                    "spent": _money(spent),
                    "remaining": _money(max(Decimal("0"), budget.amount - spent)),
                    "percent_used": round(float(spent / budget.amount * 100), 1) if budget.amount else 0,
                    "period": budget.period,
                }
            )
        return Response(data)

    def post(self, request):
        name = str(request.data.get("name", "")).strip()[:100]
        category_name = str(request.data.get("category_name", "")).strip()[:100]
        try:
            amount = Decimal(str(request.data.get("amount", ""))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return Response({"detail": "Enter a valid budget amount."}, status=status.HTTP_400_BAD_REQUEST)
        if not name:
            return Response({"detail": "Enter a name for this budget."}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"detail": "Budget amount must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            budget = Budget.objects.create(
                user=request.user,
                name=name,
                category_name=category_name,
                amount=amount,
            )
        except IntegrityError:
            return Response({"detail": "A budget with this name already exists."}, status=status.HTTP_409_CONFLICT)
        return Response({"id": budget.id, "name": budget.name}, status=status.HTTP_201_CREATED)


class BudgetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        budget = get_object_or_404(Budget, user=request.user, pk=pk)
        budget.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
