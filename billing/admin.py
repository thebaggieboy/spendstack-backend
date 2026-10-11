from django.contrib import admin

from .models import CheckoutAttempt, Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "plan_code",
        "status",
        "amount_kobo",
        "next_payment_date",
        "cancel_at_period_end",
    )
    list_filter = ("status", "cancel_at_period_end")
    search_fields = ("user__email", "subscription_code")
    exclude = ("encrypted_email_token",)


@admin.register(CheckoutAttempt)
class CheckoutAttemptAdmin(admin.ModelAdmin):
    list_display = ("user", "reference", "plan_code", "amount_kobo", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email", "reference")
