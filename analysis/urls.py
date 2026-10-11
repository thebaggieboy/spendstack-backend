from django.urls import path
from .views import (
    StatementDetailView,
    StatementListView,
    StatementPreviewConfirmView,
    StatementPreviewDeleteView,
    StatementPreviewView,
    StatementUploadView,
)

urlpatterns = [
    path('preview/', StatementPreviewView.as_view(), name='statement-preview'),
    path('preview/<int:pk>/confirm/', StatementPreviewConfirmView.as_view(), name='statement-preview-confirm'),
    path('preview/<int:pk>/', StatementPreviewDeleteView.as_view(), name='statement-preview-delete'),
    path('upload/', StatementUploadView.as_view(), name='statement-upload'),
    path('statements/', StatementListView.as_view(), name='statement-list'),
    path('statements/<int:pk>/', StatementDetailView.as_view(), name='statement-detail'),
]
