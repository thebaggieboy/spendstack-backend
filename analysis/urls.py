from django.urls import path
from .views import StatementUploadView

urlpatterns = [
    path('upload/', StatementUploadView.as_view(), name='statement-upload'),
]
