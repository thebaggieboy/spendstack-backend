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
        Transaction.objects.create(
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
            "You spent ₦1,250 at Local Cafe."
        )

        response = self.client.post(
            "/api/dashboard/chat/",
            {"question": "How much did I spend?", "statement_id": statement.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["answer"], "You spent ₦1,250 at Local Cafe.")
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
