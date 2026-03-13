from django.urls import path
from .views import StatementUploadView, StatementListView, StatementDetailView

urlpatterns = [
    path('upload/', StatementUploadView.as_view(), name='statement-upload'),
    path('statements/', StatementListView.as_view(), name='statement-list'),
    path('statements/<int:pk>/', StatementDetailView.as_view(), name='statement-detail'),
]
