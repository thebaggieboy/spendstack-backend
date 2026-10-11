from django.urls import path

from .views import (
    CancelSubscriptionView,
    CheckoutVerifyView,
    CheckoutView,
    PlanView,
    SubscriptionView,
)


urlpatterns = [
    path("plans/", PlanView.as_view(), name="billing-plans"),
    path("subscription/", SubscriptionView.as_view(), name="billing-subscription"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("checkout/verify/", CheckoutVerifyView.as_view(), name="billing-checkout-verify"),
    path("subscription/cancel/", CancelSubscriptionView.as_view(), name="billing-subscription-cancel"),
]
