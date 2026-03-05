from django.urls import path
from .views import DashboardOverviewView, CategoryBreakdownView, CashFlowView, TopMerchantsView
from .views_ai import AIAdvisorView
from .views_transactions import DashboardTransactionsView

urlpatterns = [
    path('overview/', DashboardOverviewView.as_view(), name='dashboard-overview'),
    path('category-breakdown/', CategoryBreakdownView.as_view(), name='dashboard-category-breakdown'),
    path('cash-flow/', CashFlowView.as_view(), name='dashboard-cash-flow'),
    path('top-merchants/', TopMerchantsView.as_view(), name='dashboard-top-merchants'),
    path('advisor/', AIAdvisorView.as_view(), name='dashboard-advisor'),
    path('transactions/recent/', DashboardTransactionsView.as_view(), name='dashboard-recent-txns'),
]
