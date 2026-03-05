from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/analysis/', include('analysis.urls')),
    path('api/dashboard/', include('dashboard.urls')),
]
