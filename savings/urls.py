from django.urls import path

from .views import (
    BankAccountListView,
    BankDirectoryView,
    BudgetDetailView,
    BudgetListView,
    BachsFundingStatusView,
    BachsWebhookView,
    FundingInitializeView,
    FundingVerifyView,
    LockPeriodListView,
    PaystackWebhookView,
    ScheduleListView,
    SchedulePayoutView,
    WalletView,
)

urlpatterns = [
    path("wallet/", WalletView.as_view(), name="savings-wallet"),
    path("fund/initialize/", FundingInitializeView.as_view(), name="savings-fund-initialize"),
    path("fund/verify/", FundingVerifyView.as_view(), name="savings-fund-verify"),
    path("fund/bachs/status/", BachsFundingStatusView.as_view(), name="savings-bachs-fund-status"),
    path("bank-directory/", BankDirectoryView.as_view(), name="savings-bank-directory"),
    path("bank-accounts/", BankAccountListView.as_view(), name="savings-bank-accounts"),
    path("budgets/", BudgetListView.as_view(), name="savings-budgets"),
    path("budgets/<int:pk>/", BudgetDetailView.as_view(), name="savings-budget-detail"),
    path("locks/", LockPeriodListView.as_view(), name="savings-locks"),
    path("schedules/", ScheduleListView.as_view(), name="savings-schedules"),
    path("schedules/<uuid:pk>/payout/", SchedulePayoutView.as_view(), name="savings-schedule-payout"),
    path("webhooks/paystack/", PaystackWebhookView.as_view(), name="savings-paystack-webhook"),
    path("webhooks/bachs/", BachsWebhookView.as_view(), name="savings-bachs-webhook"),
]
