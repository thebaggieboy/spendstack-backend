from django.contrib import admin

from .models import BankAccount, Budget, LockPeriod, PayoutAttempt, Schedule, Wallet, WalletTransaction


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "available_balance", "locked_balance", "updated_at")
    search_fields = ("user__email",)


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "bank_name", "account_name", "account_last4", "is_default")
    search_fields = ("user__email", "account_name", "bank_name")


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ("user", "type", "amount", "status", "reference", "created_at")
    list_filter = ("type", "status")
    search_fields = ("user__email", "reference")


@admin.register(LockPeriod)
class LockPeriodAdmin(admin.ModelAdmin):
    list_display = ("user", "amount", "status", "lock_date", "unlock_date")
    list_filter = ("status",)
    search_fields = ("user__email", "description")


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "payout_amount", "status", "scheduled_date", "auto_payout")
    list_filter = ("status", "auto_payout")
    search_fields = ("user__email", "title", "reference")


@admin.register(PayoutAttempt)
class PayoutAttemptAdmin(admin.ModelAdmin):
    list_display = ("schedule", "reference", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reference", "schedule__user__email")


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "category_name", "amount", "period", "is_active")
    list_filter = ("period", "is_active")
    search_fields = ("user__email", "name", "category_name")
