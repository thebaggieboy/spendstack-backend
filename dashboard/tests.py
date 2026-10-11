import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from analysis.models import Statement
from transactions.models import Category, Transaction


class StatementChatApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="chat-owner@example.com",
            password="safe-test-password",
        )
        self.other_user = user_model.objects.create_user(
            email="other-chat-owner@example.com",
            password="safe-test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch.dict(os.environ, {"NEXT_GEMINI_API_KEY": "unit-test-key"})
    @patch("dashboard.views_ai.genai.Client")
    def test_chat_uses_only_the_authenticated_users_statement_data(self, client_factory):
        category = Category.objects.create(name="Dining")
        statement = Statement.objects.create(user=self.user, filename="October.csv")
        transaction = Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 1),
            amount=Decimal("-1250.00"),
            merchant_name="Local Cafe",
            raw_description="Lunch",
            category=category,
        )
        Transaction.objects.create(
            user=self.other_user,
            date=date(2026, 10, 1),
            amount=Decimal("-99999.00"),
            merchant_name="Private Shop",
            raw_description="Private transaction",
            category=category,
        )
        client_factory.return_value.models.generate_content.return_value.text = (
            f"You spent ₦1,250 at Local Cafe [TX-{transaction.pk}]."
        )

        response = self.client.post(
            "/api/dashboard/chat/",
            {"question": "How much did I spend?", "statement_id": statement.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"[TX-{transaction.pk}]", response.data["answer"])
        self.assertEqual(response.data["sources"][0]["transaction_id"], transaction.pk)
        prompt = client_factory.return_value.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Local Cafe", prompt)
        self.assertNotIn("Private Shop", prompt)

    def test_chat_rejects_statements_owned_by_another_user(self):
        statement = Statement.objects.create(user=self.other_user, filename="Private.csv")
        response = self.client.post(
            "/api/dashboard/chat/",
            {"question": "Summarize this statement", "statement_id": statement.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_chat_requires_a_question(self):
        response = self.client.post("/api/dashboard/chat/", {"question": "  "}, format="json")
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views_insights.timezone.localdate", return_value=date(2026, 3, 15))
    def test_monthly_comparison_uses_matching_days_and_excludes_transfers(self, _today):
        category = Category.objects.create(name="Comparison dining")
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 3, 10),
            amount=Decimal("-500.00"),
            merchant_name="Cafe",
            raw_description="Cafe",
            category=category,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 2, 10),
            amount=Decimal("-300.00"),
            merchant_name="Cafe",
            raw_description="Cafe",
            category=category,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 3, 12),
            amount=Decimal("-9999.00"),
            merchant_name="Own account",
            raw_description="Transfer",
            is_transfer=True,
        )

        response = self.client.get("/api/dashboard/monthly-comparison/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["current_period"], {"start": "2026-03-01", "end": "2026-03-15"})
        self.assertEqual(response.data["previous_period"], {"start": "2026-02-01", "end": "2026-02-15"})
        self.assertEqual(response.data["expenses"]["current"], 500.0)
        self.assertEqual(response.data["expenses"]["previous"], 300.0)
        self.assertEqual(response.data["expenses"]["change"]["percent"], 66.7)
        self.assertEqual(response.data["categories"][0]["category"], "Comparison dining")
