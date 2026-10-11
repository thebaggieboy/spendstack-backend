from django.urls import path
from .views import DashboardOverviewView, CategoryBreakdownView, CashFlowView, TopMerchantsView
from .views_ai import AIAdvisorView, StatementChatView
from .views_transactions import DashboardTransactionsView
from .views_insights import MonthlyComparisonView, SpendingInsightsView, MonthlyTrendView
from transactions.tools_views import RecurringBillsView

urlpatterns = [
    path('overview/', DashboardOverviewView.as_view(), name='dashboard-overview'),
    path('category-breakdown/', CategoryBreakdownView.as_view(), name='dashboard-category-breakdown'),
    path('cash-flow/', CashFlowView.as_view(), name='dashboard-cash-flow'),
    path('top-merchants/', TopMerchantsView.as_view(), name='dashboard-top-merchants'),
    path('advisor/', AIAdvisorView.as_view(), name='dashboard-advisor'),
    path('chat/', StatementChatView.as_view(), name='dashboard-statement-chat'),
    path('transactions/recent/', DashboardTransactionsView.as_view(), name='dashboard-recent-txns'),
    path('insights/', SpendingInsightsView.as_view(), name='dashboard-insights'),
    path('monthly-trend/', MonthlyTrendView.as_view(), name='dashboard-monthly-trend'),
    path('monthly-comparison/', MonthlyComparisonView.as_view(), name='dashboard-monthly-comparison'),
    path('recurring-bills/', RecurringBillsView.as_view(), name='dashboard-recurring-bills'),
]
