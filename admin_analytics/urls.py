from django.urls import path

from .views import AdminAnalyticsOverviewView, AdminAnalyticsUsersView, TrackPageVisitView

urlpatterns = [
    path("track/", TrackPageVisitView.as_view(), name="track-page-visit"),
    path("overview/", AdminAnalyticsOverviewView.as_view(), name="admin-analytics-overview"),
    path("users/", AdminAnalyticsUsersView.as_view(), name="admin-analytics-users"),
]
