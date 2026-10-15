import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from .bachs import BachsError, create_payout, get_payout_destination
from .models import BankAccount, LockPeriod, PayoutAttempt, Schedule, Wallet, WalletTransaction
from .paystack import PaystackError, paystack_request


KOBO_PER_NAIRA = Decimal("100")


def to_kobo(amount):
    return int((Decimal(amount) * KOBO_PER_NAIRA).quantize(Decimal("1")))


def next_occurrence(current, recurrence, timezone_name):
    if recurrence not in (Schedule.DAILY, Schedule.WEEKLY, Schedule.MONTHLY):
        return None
    zone = ZoneInfo(timezone_name)
    local = current.astimezone(zone)
    naive = local.replace(tzinfo=None)
    if recurrence == Schedule.DAILY:
        naive += timedelta(days=1)
    elif recurrence == Schedule.WEEKLY:
        naive += timedelta(weeks=1)
    else:
        month = naive.month % 12 + 1
        year = naive.year + (naive.month == 12)
        naive = naive.replace(
            year=year,
            month=month,
            day=min(naive.day, monthrange(year, month)[1]),
        )

    for _ in range(181):
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
                return candidate
        naive += timedelta(minutes=1)
    raise ValueError("Could not resolve the next local payout time.")


def credit_verified_funding(reference, verified_data):
    with transaction.atomic():
        wallet_transaction = (
            WalletTransaction.objects.select_for_update()
            .select_related("user")
            .filter(reference=reference, type=WalletTransaction.DEPOSIT)
            .first()
        )
        if not wallet_transaction:
            return False
        if wallet_transaction.status == WalletTransaction.COMPLETED:
            return True
        if wallet_transaction.status != WalletTransaction.PENDING:
            return False

        expected_kobo = to_kobo(wallet_transaction.amount)
        if verified_data.get("status") != "success":
            return False
        customer = verified_data.get("customer")
        if not isinstance(customer, dict):
            customer = {}
        if (
            verified_data.get("currency") != "NGN"
            or verified_data.get("amount") != expected_kobo
            or verified_data.get("reference") != reference
            or str(customer.get("email", "")).lower()
            != wallet_transaction.user.email.lower()
        ):
            wallet_transaction.status = WalletTransaction.FAILED
            wallet_transaction.metadata = {"verification": "amount_or_customer_mismatch"}
            wallet_transaction.save(update_fields=["status", "metadata"])
            return False

        wallet, _ = Wallet.objects.get_or_create(user=wallet_transaction.user)
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        balance_before = wallet.available_balance
        wallet.available_balance += wallet_transaction.amount
        wallet.save(update_fields=["available_balance", "updated_at"])

        wallet_transaction.balance_before = balance_before
        wallet_transaction.balance_after = wallet.available_balance
        wallet_transaction.status = WalletTransaction.COMPLETED
        wallet_transaction.completed_at = timezone.now()
        wallet_transaction.metadata = {
            "channel": verified_data.get("channel"),
            "gateway_response": verified_data.get("gateway_response"),
        }
        wallet_transaction.save(
            update_fields=[
                "balance_before",
                "balance_after",
                "status",
                "completed_at",
                "metadata",
            ]
        )
        return True


def credit_bachs_funding(reference, event_data):
    with transaction.atomic():
        wallet_transaction = (
            WalletTransaction.objects.select_for_update()
            .select_related("user")
            .filter(reference=reference, type=WalletTransaction.DEPOSIT)
            .first()
        )
        if not wallet_transaction:
            return False
        metadata = wallet_transaction.metadata or {}
        if metadata.get("payment_provider") != "bachs":
            return False
        if wallet_transaction.status == WalletTransaction.COMPLETED:
            return True
        if wallet_transaction.status != WalletTransaction.PENDING:
            return False

        try:
            amount = Decimal(str(event_data.get("amount", "")))
        except (InvalidOperation, TypeError, ValueError):
            amount = None
        if (
            amount is None
            or not amount.is_finite()
            or event_data.get("status") != "SUCCEEDED"
            or event_data.get("currency") != "NGN"
            or event_data.get("reference") != reference
            or amount != wallet_transaction.amount
        ):
            wallet_transaction.status = WalletTransaction.FAILED
            wallet_transaction.metadata = {
                **metadata,
                "verification": "amount_currency_or_reference_mismatch",
            }
            wallet_transaction.save(update_fields=["status", "metadata"])
            return False

        wallet, _ = Wallet.objects.get_or_create(user=wallet_transaction.user)
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        balance_before = wallet.available_balance
        wallet.available_balance += wallet_transaction.amount
        wallet.save(update_fields=["available_balance", "updated_at"])

        wallet_transaction.balance_before = balance_before
        wallet_transaction.balance_after = wallet.available_balance
        wallet_transaction.status = WalletTransaction.COMPLETED
        wallet_transaction.completed_at = timezone.now()
        wallet_transaction.metadata = {
            **metadata,
            "verification": "signed_bachs_collection_succeeded",
            "settlement_amount": event_data.get("settlement_amount"),
            "settlement_currency": event_data.get("settlement_currency"),
            "processing_fee": event_data.get("processing_fee"),
            "processing_fee_currency": event_data.get("processing_fee_currency"),
        }
        wallet_transaction.save(
            update_fields=[
                "balance_before",
                "balance_after",
                "status",
                "completed_at",
                "metadata",
            ]
        )
        return True


def fail_pending_funding(reference, provider):
    with transaction.atomic():
        wallet_transaction = (
            WalletTransaction.objects.select_for_update()
            .filter(
                reference=reference,
                type=WalletTransaction.DEPOSIT,
                status=WalletTransaction.PENDING,
            )
            .first()
        )
        if not wallet_transaction:
            return False
        if provider == Schedule.BACHS:
            if (wallet_transaction.metadata or {}).get("payment_provider") != Schedule.BACHS:
                return False
        elif (wallet_transaction.metadata or {}).get("payment_provider", Schedule.PAYSTACK) != Schedule.PAYSTACK:
            return False
        wallet_transaction.status = WalletTransaction.FAILED
        wallet_transaction.metadata = {
            **(wallet_transaction.metadata or {}),
            "verification": "provider_confirmed_failure",
        }
        wallet_transaction.completed_at = timezone.now()
        wallet_transaction.save(update_fields=["status", "metadata", "completed_at"])
        return True


def _settle_payout_attempt(attempt, schedule, lock_period, *, succeeded, failure_reason=""):
    now = timezone.now()
    wallet = Wallet.objects.select_for_update().get(user=schedule.user)
    if succeeded:
        if attempt.status == "reversed":
            return False
        if attempt.status == Schedule.COMPLETED:
            return True
        if schedule.remaining_amount < attempt.amount or wallet.locked_balance < attempt.amount:
            return False

        locked_before = wallet.locked_balance
        wallet.locked_balance -= attempt.amount
        schedule.remaining_amount -= attempt.amount
        wallet.save(update_fields=["locked_balance", "updated_at"])
        attempt.status = Schedule.COMPLETED
        attempt.completed_at = now
        schedule.executed_at = now
        if schedule.remaining_amount > 0 and schedule.recurrence:
            schedule.scheduled_date = next_occurrence(
                schedule.scheduled_date,
                schedule.recurrence,
                schedule.timezone_name,
            )
            schedule.status = Schedule.PENDING
            lock_period.status = LockPeriod.LOCKED
        else:
            schedule.remaining_amount = Decimal("0.00")
            schedule.status = Schedule.COMPLETED
            lock_period.status = LockPeriod.UNLOCKED
            lock_period.actual_unlock_date = now
        attempt.save(update_fields=["status", "completed_at"])
        schedule.save(
            update_fields=[
                "remaining_amount",
                "scheduled_date",
                "status",
                "executed_at",
            ]
        )
        lock_period.save(update_fields=["status", "actual_unlock_date"])
        WalletTransaction.objects.create(
            user=schedule.user,
            type=WalletTransaction.TRANSFER,
            amount=attempt.amount,
            balance_before=wallet.available_balance,
            balance_after=wallet.available_balance,
            status=WalletTransaction.COMPLETED,
            reference=f"transfer-{attempt.reference}",
            description=f"Payout for {schedule.title}",
            metadata={
                "schedule_id": str(schedule.id),
                "attempt_id": str(attempt.id),
                "provider": attempt.provider,
                "provider_reference": attempt.provider_reference or attempt.reference,
                "locked_balance_before": str(locked_before),
                "locked_balance_after": str(wallet.locked_balance),
            },
            completed_at=now,
        )
        return True

    if attempt.status in (Schedule.COMPLETED, "reversed"):
        return False
    attempt.status = Schedule.FAILED
    attempt.failure_reason = failure_reason[:255]
    attempt.completed_at = now
    schedule.status = Schedule.FAILED
    lock_period.status = LockPeriod.LOCKED
    attempt.save(update_fields=["status", "failure_reason", "completed_at"])
    schedule.save(update_fields=["status"])
    lock_period.save(update_fields=["status"])
    return True


def finalize_payout_attempt(reference, event, data):
    with transaction.atomic():
        attempt = (
            PayoutAttempt.objects.select_for_update()
            .select_related("schedule__lock_period", "schedule__user")
            .filter(reference=reference)
            .first()
        )
        if not attempt:
            return False
        if data.get("amount") != to_kobo(attempt.amount):
            return False

        schedule = Schedule.objects.select_for_update().get(pk=attempt.schedule_id)
        lock_period = LockPeriod.objects.select_for_update().get(pk=schedule.lock_period_id)
        recipient = data.get("recipient")
        if (
            isinstance(recipient, dict)
            and recipient.get("recipient_code")
            and recipient["recipient_code"] != schedule.bank_account.recipient_code
        ):
            return False
        if attempt.status == "reversed":
            return event == "transfer.reversed"
        if attempt.status == Schedule.COMPLETED:
            if event == "transfer.success":
                return True
            if event != "transfer.reversed":
                return False

            wallet = Wallet.objects.select_for_update().get(user=schedule.user)
            locked_before = wallet.locked_balance
            wallet.locked_balance += attempt.amount
            wallet.save(update_fields=["locked_balance", "updated_at"])
            WalletTransaction.objects.create(
                user=schedule.user,
                type=WalletTransaction.REVERSAL,
                amount=attempt.amount,
                balance_before=wallet.available_balance,
                balance_after=wallet.available_balance,
                status=WalletTransaction.COMPLETED,
                reference=f"reversal-{reference}",
                description=f"Reversed payout for {schedule.title}",
                metadata={
                    "locked_balance_before": str(locked_before),
                    "locked_balance_after": str(wallet.locked_balance),
                    "paystack_reference": reference,
                },
                completed_at=timezone.now(),
            )
            attempt.status = "reversed"
            schedule.status = Schedule.FAILED
            lock_period.status = LockPeriod.PENDING
            attempt.save(update_fields=["status"])
            schedule.save(update_fields=["status"])
            lock_period.save(update_fields=["status"])
            return True

        attempt.completed_at = timezone.now()
        if event == "transfer.success":
            if schedule.status == Schedule.COMPLETED:
                attempt.status = Schedule.COMPLETED
                attempt.save(update_fields=["status", "completed_at"])
                return True
            wallet = Wallet.objects.select_for_update().get(user=schedule.user)
            if wallet.locked_balance < attempt.amount:
                return False
            wallet.locked_balance -= attempt.amount
            wallet.save(update_fields=["locked_balance", "updated_at"])

            WalletTransaction.objects.create(
                user=schedule.user,
                type=WalletTransaction.UNLOCK,
                amount=attempt.amount,
                balance_before=wallet.available_balance,
                balance_after=wallet.available_balance,
                status=WalletTransaction.COMPLETED,
                reference=f"unlock-{reference}",
                description=f"Payout for {schedule.title}",
                metadata={
                    "locked_balance_after": str(wallet.locked_balance),
                    "paystack_reference": reference,
                },
                completed_at=timezone.now(),
            )
            attempt.status = Schedule.COMPLETED
            schedule.status = Schedule.COMPLETED
            schedule.executed_at = timezone.now()
            lock_period.status = LockPeriod.UNLOCKED
            lock_period.actual_unlock_date = timezone.now()
            attempt.save(update_fields=["status", "completed_at"])
            schedule.save(update_fields=["status", "executed_at"])
            lock_period.save(update_fields=["status", "actual_unlock_date"])
            return True

        attempt.status = Schedule.FAILED
        schedule.status = Schedule.FAILED
        lock_period.status = LockPeriod.PENDING
        attempt.save(update_fields=["status", "completed_at"])
        schedule.save(update_fields=["status"])
        lock_period.save(update_fields=["status"])
        return True


def initiate_schedule_payout(schedule_id, *, allow_early=False):
    with transaction.atomic():
        schedule = (
            Schedule.objects.select_for_update()
            .select_related("bank_account", "user", "lock_period")
            .get(pk=schedule_id)
        )
        if schedule.status == Schedule.PROCESSING:
            raise PaystackError("A payout is already processing for this schedule.")
        if schedule.status == Schedule.REQUIRES_ACTION:
            raise PaystackError(
                "This transfer needs Paystack account approval. Do not retry it; complete the transfer in Paystack."
            )
        if schedule.status in (Schedule.COMPLETED, Schedule.CANCELLED):
            raise PaystackError("This payout can no longer be started.")
        if not allow_early and schedule.scheduled_date > timezone.now():
            raise PaystackError("This payout can only be started on or after its scheduled date.")
        if schedule.lock_period.status == LockPeriod.UNLOCKED:
            raise PaystackError("The lock has already been paid out.")

        reference = f"SS-PAYOUT-{uuid.uuid4().hex}"
        attempt = PayoutAttempt.objects.create(
            schedule=schedule,
            reference=reference,
            amount=schedule.payout_amount,
        )
        schedule.status = Schedule.PROCESSING
        schedule.reference = reference
        schedule.lock_period.status = LockPeriod.PENDING
        schedule.save(update_fields=["status", "reference"])
        schedule.lock_period.save(update_fields=["status"])

    try:
        response_data = paystack_request(
            "POST",
            "/transfer",
            payload={
                "source": "balance",
                "amount": to_kobo(attempt.amount),
                "recipient": schedule.bank_account.recipient_code,
                "reason": f"Spendstack scheduled payout: {schedule.title}"[:100],
                "reference": reference,
            },
        )
    except PaystackError:
        # A timeout may occur after Paystack accepted the transfer; preserve the
        # processing state so a retry cannot accidentally send a second payout.
        raise

    transfer_status = response_data.get("status")
    if transfer_status == "otp":
        with transaction.atomic():
            attempt = PayoutAttempt.objects.select_for_update().get(pk=attempt.pk)
            schedule = Schedule.objects.select_for_update().get(pk=schedule.pk)
            if schedule.status == Schedule.PROCESSING:
                attempt.status = Schedule.REQUIRES_ACTION
                schedule.status = Schedule.REQUIRES_ACTION
                attempt.transfer_code = response_data.get("transfer_code", "")
                attempt.save(update_fields=["status", "transfer_code"])
                schedule.save(update_fields=["status"])
    else:
        PayoutAttempt.objects.filter(pk=attempt.pk).update(
            transfer_code=response_data.get("transfer_code", "")
        )

    return Schedule.objects.select_related("bank_account", "user", "lock_period").get(pk=schedule.pk), response_data
