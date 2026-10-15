import uuid

from django.conf import settings
from django.db import models


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_wallet",
    )
    available_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    locked_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_balance(self):
        return self.available_balance + self.locked_balance


class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_bank_accounts",
    )
    bank_name = models.CharField(max_length=120)
    bank_code = models.CharField(max_length=20)
    account_name = models.CharField(max_length=160)
    account_last4 = models.CharField(max_length=4)
    recipient_code = models.CharField(max_length=80, unique=True, null=True, blank=True)
    bachs_destination_id = models.CharField(max_length=80, unique=True, null=True, blank=True)
    bachs_is_usable = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]


class WalletTransaction(models.Model):
    DEPOSIT = "deposit"
    LOCK = "lock"
    UNLOCK = "unlock"
    TRANSFER = "transfer"
    REVERSAL = "reversal"
    TYPES = [(value, value.title()) for value in (DEPOSIT, LOCK, UNLOCK, TRANSFER, REVERSAL)]

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUSES = [(value, value.title()) for value in (PENDING, COMPLETED, FAILED)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_transactions",
    )
    type = models.CharField(max_length=16, choices=TYPES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    balance_before = models.DecimalField(max_digits=15, decimal_places=2)
    balance_after = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUSES, default=PENDING)
    reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user"], name="sav_wtx_user_42b8d1_idx"),
            models.Index(fields=["type"], name="sav_wtx_type_2e68d0_idx"),
            models.Index(fields=["status"], name="sav_wtx_status_4c4b17_idx"),
        ]


class LockPeriod(models.Model):
    LOCKED = "locked"
    PENDING = "pending"
    UNLOCKED = "unlocked"
    CANCELLED = "cancelled"
    STATUSES = [(value, value.title()) for value in (LOCKED, PENDING, UNLOCKED, CANCELLED)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lock_periods",
    )
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    lock_date = models.DateTimeField()
    unlock_date = models.DateTimeField()
    actual_unlock_date = models.DateTimeField(null=True, blank=True)
    interval_days = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUSES, default=LOCKED)
    description = models.CharField(max_length=255, blank=True)
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name="lock_periods",
    )
    payout_reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["unlock_date", "-created_at"]
        indexes = [
            models.Index(fields=["user"], name="sav_lck_user_43c2f1_idx"),
            models.Index(fields=["status"], name="sav_lck_status_67f943_idx"),
            models.Index(fields=["unlock_date"], name="sav_lck_unlock_e2bc12_idx"),
        ]


class Schedule(models.Model):
    PENDING = "pending"
    PROCESSING = "processing"
    REQUIRES_ACTION = "requires_action"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    PAYSTACK = "paystack"
    BACHS = "bachs"
    STATUSES = [
        (value, value.replace("_", " ").title())
        for value in (PENDING, PROCESSING, REQUIRES_ACTION, COMPLETED, FAILED, CANCELLED, PAUSED)
    ]
    RECURRENCES = [
        (DAILY, "Daily"),
        (WEEKLY, "Weekly"),
        (MONTHLY, "Monthly"),
    ]
    PAYOUT_PROVIDERS = [(PAYSTACK, "Paystack"), (BACHS, "Bachs")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_schedules",
    )
    lock_period = models.OneToOneField(
        LockPeriod,
        on_delete=models.CASCADE,
        related_name="payout_schedule",
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name="payout_schedules",
    )
    title = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payout_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    remaining_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    scheduled_date = models.DateTimeField()
    status = models.CharField(max_length=24, choices=STATUSES, default=PENDING)
    recurrence = models.CharField(max_length=20, choices=RECURRENCES, null=True, blank=True)
    timezone_name = models.CharField(max_length=64, default="UTC")
    payout_provider = models.CharField(max_length=12, choices=PAYOUT_PROVIDERS, default=PAYSTACK)
    auto_payout = models.BooleanField(default=True)
    reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["scheduled_date", "-created_at"]
        indexes = [
            models.Index(fields=["user"], name="sav_sch_user_11fa82_idx"),
            models.Index(fields=["scheduled_date"], name="sav_sch_date_8101d4_idx"),
        ]


class PayoutAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    reference = models.CharField(max_length=100, unique=True)
    transfer_code = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=24, default=Schedule.PROCESSING)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    provider = models.CharField(max_length=12, choices=Schedule.PAYOUT_PROVIDERS, default=Schedule.PAYSTACK)
    provider_reference = models.CharField(max_length=100, null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Budget(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="spending_budgets",
    )
    name = models.CharField(max_length=100)
    category_name = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    period = models.CharField(max_length=12, default="monthly")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                name="unique_user_spending_budget_name",
            )
        ]
