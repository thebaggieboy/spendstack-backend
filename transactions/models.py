from django.db import models
from django.conf import settings

class Account(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='accounts')
    name = models.CharField(max_length=100)
    bank_name = models.CharField(max_length=100, blank=True)
    mask = models.CharField(max_length=4, blank=True)

    def __str__(self):
        return f"{self.name} - {self.user.username}"


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    color = models.CharField(max_length=20, default="#8b5cf6") # For frontend UI
    is_system = models.BooleanField(default=True) # True for default categories
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Transaction(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transactions')
    statement = models.ForeignKey('analysis.Statement', on_delete=models.CASCADE, null=True, blank=True, related_name='transactions')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    
    date = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2) 
    # Convention: negative for expense, positive for income
    
    merchant_name = models.CharField(max_length=255, blank=True)
    raw_description = models.TextField()
    original_merchant_name = models.CharField(max_length=255, blank=True)
    original_category_name = models.CharField(max_length=100, blank=True)
    is_transfer = models.BooleanField(default=False, db_index=True)
    
    is_pending = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.date} - {self.merchant_name or self.raw_description[:20]} - {self.amount}"


class TransactionRule(models.Model):
    MATCH_DESCRIPTION = "description"
    MATCH_MERCHANT = "merchant"
    MATCH_EITHER = "either"
    MATCH_FIELDS = [
        (MATCH_DESCRIPTION, "Description"),
        (MATCH_MERCHANT, "Merchant"),
        (MATCH_EITHER, "Description or merchant"),
    ]

    TRANSFER_AUTO = "automatic"
    TRANSFER_YES = "transfer"
    TRANSFER_NO = "not_transfer"
    TRANSFER_ACTIONS = [
        (TRANSFER_AUTO, "Use description detection"),
        (TRANSFER_YES, "Always mark as transfer"),
        (TRANSFER_NO, "Never mark as transfer"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transaction_rules",
    )
    match_text = models.CharField(max_length=255)
    match_field = models.CharField(
        max_length=20,
        choices=MATCH_FIELDS,
        default=MATCH_DESCRIPTION,
    )
    merchant_name = models.CharField(max_length=255, blank=True)
    category_name = models.CharField(max_length=100, blank=True)
    transfer_action = models.CharField(
        max_length=20,
        choices=TRANSFER_ACTIONS,
        default=TRANSFER_AUTO,
    )
    priority = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-priority", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "match_field", "match_text"],
                name="unique_user_transaction_rule",
            )
        ]

    def __str__(self):
        return f"{self.match_text} ({self.user})"


class SpendingAlert(models.Model):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    PERIODS = [
        (WEEKLY, "Weekly"),
        (MONTHLY, "Monthly"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="spending_alerts",
    )
    name = models.CharField(max_length=100, blank=True)
    category_name = models.CharField(max_length=100, blank=True)
    threshold = models.DecimalField(max_digits=12, decimal_places=2)
    period = models.CharField(max_length=10, choices=PERIODS, default=MONTHLY)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["period", "category_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "category_name", "period"],
                name="unique_user_spending_alert",
            )
        ]

    def __str__(self):
        scope = self.category_name or "All spending"
        return f"{scope} {self.period} alert for {self.user}"
