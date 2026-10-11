from django.urls import path
from .views import TransactionListView
from .tools_views import (
    SpendingAlertDetailView,
    SpendingAlertListView,
    TransactionRuleDetailView,
    TransactionRuleListView,
)

urlpatterns = [
    path('', TransactionListView.as_view(), name='transaction-list'),
    path('rules/', TransactionRuleListView.as_view(), name='transaction-rule-list'),
    path('rules/<int:pk>/', TransactionRuleDetailView.as_view(), name='transaction-rule-detail'),
    path('alerts/', SpendingAlertListView.as_view(), name='spending-alert-list'),
    path('alerts/<int:pk>/', SpendingAlertDetailView.as_view(), name='spending-alert-detail'),
]
