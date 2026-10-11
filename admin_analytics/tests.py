from datetime import timedelta
import uuid

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import PageVisit

User = get_user_model()


class AdminAnalyticsApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="analytics-admin@example.com",
            password="test-password-123",
        )
        self.user = User.objects.create_user(
            email="analytics-user@example.com",
            password="test-password-123",
        )

    def test_tracking_records_minimal_page_data_and_associates_authenticated_user(self):
        self.client.force_authenticate(self.user)
        visitor_id = str(uuid.uuid4())

        response = self.client.post(
            "/api/internal-analytics/track/",
            {"visitor_id": visitor_id, "path": "/dashboard"},
            format="json",
        )

        self.assertEqual(response.status_code, 202)
        visit = PageVisit.objects.get()
        self.assertEqual(visit.user, self.user)
        self.assertEqual(visit.visitor_id, uuid.UUID(visitor_id))
        self.assertEqual(visit.path, "/dashboard")

    def test_tracking_rejects_query_strings_and_external_urls(self):
        response = self.client.post(
            "/api/internal-analytics/track/",
            {"visitor_id": str(uuid.uuid4()), "path": "https://example.com/?email=x"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PageVisit.objects.count(), 0)

    def test_non_admin_cannot_read_overview_or_user_list(self):
        self.client.force_authenticate(self.user)

        self.assertEqual(
            self.client.get("/api/internal-analytics/overview/").status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/internal-analytics/users/").status_code,
            403,
        )

    def test_admin_can_read_overview_and_paginated_user_list(self):
        PageVisit.objects.create(
            user=self.user,
            visitor_id=uuid.uuid4(),
            path="/dashboard",
            created_at=timezone.now() - timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin)

        overview = self.client.get("/api/internal-analytics/overview/")
        users = self.client.get("/api/internal-analytics/users/?search=analytics-user")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.data["users"]["active_1d"], 1)
        self.assertEqual(overview.data["activity"]["page_views_30d"], 1)
        self.assertEqual(overview.data["activity"]["top_pages"][0]["path"], "/dashboard")
        self.assertEqual(users.status_code, 200)
        self.assertEqual(users.data["count"], 1)
        self.assertEqual(users.data["results"][0]["email"], self.user.email)
