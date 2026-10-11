import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import LockPeriod, PayoutAttempt, Schedule, Wallet, WalletTransaction
from .paystack import PaystackError, paystack_request


KOBO_PER_NAIRA = Decimal("100")


def to_kobo(amount):
    return int((Decimal(amount) * KOBO_PER_NAIRA).quantize(Decimal("1")))


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

        expected_kobo = to_kobo(wallet_transaction.amount)
        if verified_data.get("status") != "success":
            return False
        if (
            verified_data.get("currency") != "NGN"
            or verified_data.get("amount") != expected_kobo
            or verified_data.get("reference") != reference
            or verified_data.get("customer", {}).get("email", "").lower()
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
