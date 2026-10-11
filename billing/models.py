import uuid

from django.conf import settings
from django.db import models


class Subscription(models.Model):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    STATUSES = [
        (ACTIVE, "Active"),
        (PAST_DUE, "Past due"),
        (CANCELLED, "Cancelled"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_subscription",
    )
    plan_code = models.CharField(max_length=80)
    subscription_code = models.CharField(max_length=100, unique=True)
    encrypted_email_token = models.TextField()
    status = models.CharField(max_length=16, choices=STATUSES, default=ACTIVE)
    amount_kobo = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="NGN")
    next_payment_date = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def has_access(self, *, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        if self.status != self.ACTIVE:
            return False
        return not self.cancel_at_period_end or (
            self.next_payment_date is not None and now < self.next_payment_date
        )


class CheckoutAttempt(models.Model):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUSES = [(PENDING, "Pending"), (COMPLETED, "Completed"), (FAILED, "Failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_checkout_attempts",
    )
    reference = models.CharField(max_length=100, unique=True)
    plan_code = models.CharField(max_length=80)
    amount_kobo = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=STATUSES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"], name="bill_checkout_user_st_idx")]
