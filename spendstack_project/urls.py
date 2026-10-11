from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/users/', include('users.urls')),
    path('api/analysis/', include('analysis.urls')),
    path('api/dashboard/', include('dashboard.urls')),
    path('api/transactions/', include('transactions.urls')),
    path('api/savings/', include('savings.urls')),
    path('api/billing/', include('billing.urls')),
    path('api/internal-analytics/', include('admin_analytics.urls')),
]
