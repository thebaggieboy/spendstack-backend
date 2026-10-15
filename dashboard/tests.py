import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from analysis.models import Statement, StatementImportAttempt
from transactions.models import Account, Category, Transaction


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

    def test_overview_includes_transfer_totals_but_excludes_pending_rows(self):
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("100.10"),
            merchant_name="Employer",
            raw_description="Salary",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("200.00"),
            merchant_name="Refund",
            raw_description="Refund",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("-30.00"),
            merchant_name="Cafe",
            raw_description="Cafe",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("-70.00"),
            merchant_name="Market",
            raw_description="Groceries",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("500000.00"),
            merchant_name="Own Account",
            raw_description="Transfer to own account",
            is_transfer=True,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 1),
            amount=Decimal("900000.00"),
            merchant_name="Pending Credit",
            raw_description="Pending deposit",
            is_pending=True,
        )

        response = self.client.get("/api/dashboard/overview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["credits_all_time"], 500300.10)
        self.assertEqual(response.data["expenses_all_time"], 100.00)
        self.assertEqual(response.data["net_cash_flow"], 500200.10)
        self.assertAlmostEqual(response.data["savings_rate"], 500200.10 / 500300.10 * 100)
        self.assertEqual(response.data["transaction_credits_all_time"], 500300.10)
        self.assertFalse(response.data["statement_totals_mismatch"])
        self.assertEqual(response.data["highest_daily_spend"], 100.00)
        self.assertEqual(response.data["top_payee"], "Market")
        self.assertEqual(response.data["top_payee_amount"], 70.00)
        self.assertEqual(response.data["latest_statement_metrics"], {
            "opening_balance": None,
            "closing_balance": None,
            "total_credits": None,
            "total_debits": None,
            "credit_count": None,
            "debit_count": None,
            "filename": None,
            "ambiguous_fields": [],
            "incomplete_fields": [],
            "comparison": {
                "total_credits": {"status": "unavailable", "transaction_rows": "0.00", "difference": None},
                "total_debits": {"status": "unavailable", "transaction_rows": "0.00", "difference": None},
                "credit_count": {"status": "unavailable", "transaction_rows": 0, "difference": None},
                "debit_count": {"status": "unavailable", "transaction_rows": 0, "difference": None},
            },
        })
        self.assertNotIn("total_balance", response.data)

    def test_overview_uses_transaction_rows_and_exposes_reported_statement_discrepancies(self):
        statement = Statement.objects.create(
            user=self.user,
            filename="bank.xlsx",
            summary_data={"total_credits": "100.00", "total_debits": "200.00"},
        )
        statement_without_summary = Statement.objects.create(
            user=self.user,
            filename="additional.csv",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 1),
            amount=Decimal("120.00"),
            raw_description="Credit",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 1),
            amount=Decimal("-180.00"),
            raw_description="Debit",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement_without_summary,
            date=date(2026, 10, 2),
            amount=Decimal("50.00"),
            raw_description="Additional credit",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement_without_summary,
            date=date(2026, 10, 2),
            amount=Decimal("-40.00"),
            raw_description="Additional debit",
        )

        response = self.client.get("/api/dashboard/overview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["credits_all_time"], 170.00)
        self.assertEqual(response.data["expenses_all_time"], 220.00)
        self.assertEqual(response.data["transaction_credits_all_time"], 170.00)
        self.assertEqual(response.data["transaction_expenses_all_time"], 220.00)
        self.assertTrue(response.data["statement_totals_mismatch"])
        self.assertEqual(response.data["statement_totals_mismatch_fields"], ["credits", "debits"])
        self.assertEqual(response.data["statement_totals_comparison"], {
            "credits": {
                "reported": 100.00,
                "transaction_rows": 120.00,
                "statement_count": 1,
                "mismatch": True,
            },
            "debits": {
                "reported": 200.00,
                "transaction_rows": 180.00,
                "statement_count": 1,
                "mismatch": True,
            },
        })
        self.assertEqual(response.data["net_cash_flow"], -50.00)

    def test_overview_returns_statement_reported_balances_and_totals_separately(self):
        statement = Statement.objects.create(
            user=self.user,
            filename="latest.xlsx",
            summary_data={
                "opening_balance": "1200.00",
                "closing_balance": "950.00",
                "total_credits": "400.00",
                "total_debits": "650.00",
                "credit_count": "3",
                "debit_count": "7",
            },
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 1),
            amount=Decimal("390.00"),
            raw_description="Credit row",
        )

        response = self.client.get("/api/dashboard/overview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["latest_statement_metrics"], {
            "opening_balance": "1200.00",
            "closing_balance": "950.00",
            "total_credits": "400.00",
            "total_debits": "650.00",
            "credit_count": "3",
            "debit_count": "7",
            "filename": "latest.xlsx",
            "ambiguous_fields": [],
            "incomplete_fields": [],
            "comparison": {
                "total_credits": {
                    "status": "mismatch",
                    "transaction_rows": "390.00",
                    "difference": "-10.00",
                },
                "total_debits": {
                    "status": "mismatch",
                    "transaction_rows": "0.00",
                    "difference": "-650.00",
                },
                "credit_count": {
                    "status": "mismatch",
                    "transaction_rows": 1,
                    "difference": -2,
                },
                "debit_count": {
                    "status": "mismatch",
                    "transaction_rows": 0,
                    "difference": -7,
                },
            },
        })
        self.assertEqual(response.data["credits_all_time"], 390.00)

    @patch("dashboard.views_insights.timezone.localdate", return_value=date(2026, 10, 13))
    def test_insights_filters_period_and_finds_most_frequent_transfer_recipient(self, _today):
        food = Category.objects.create(name="Test food")
        travel = Category.objects.create(name="Test travel")
        for amount in ("-300.00", "-200.00"):
            Transaction.objects.create(
                user=self.user,
                date=date(2026, 10, 2),
                amount=Decimal(amount),
                merchant_name="Cafe",
                raw_description="Cafe purchase",
                category=food,
            )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 3),
            amount=Decimal("-400.00"),
            merchant_name="Taxi",
            raw_description="Taxi ride",
            category=travel,
        )
        for amount in ("-8000.00", "-3000.00", "-2000.00"):
            Transaction.objects.create(
                user=self.user,
                date=date(2026, 10, 4),
                amount=Decimal(amount),
                merchant_name="Alex",
                raw_description="NIP transfer to Alex",
                is_transfer=True,
            )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 5),
            amount=Decimal("-40000.00"),
            merchant_name="Morgan",
            raw_description="NIP transfer to Morgan",
            is_transfer=True,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 6, 5),
            amount=Decimal("-9000.00"),
            merchant_name="Alex",
            raw_description="Older NIP transfer to Alex",
            is_transfer=True,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 5),
            amount=Decimal("-10000.00"),
            merchant_name="Pending recipient",
            raw_description="Pending transfer",
            is_transfer=True,
            is_pending=True,
        )

        response = self.client.get("/api/dashboard/insights/?period=90days")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_data"])
        self.assertEqual(response.data["period_start"], "2026-07-16")
        self.assertEqual(response.data["period_end"], "2026-10-13")
        self.assertEqual(response.data["top_recipient"], "Alex")
        self.assertEqual(response.data["top_recipient_transfer_count"], 3)
        self.assertEqual(response.data["top_recipient_transfer_amount"], 13000.00)
        self.assertEqual(response.data["top_category"], "Test food")
        self.assertEqual(response.data["top_category_amount"], 500.00)
        self.assertEqual(response.data["top_category_share_percent"], 55.6)
        self.assertEqual(response.data["expense_txn_count"], 7)
        self.assertEqual(response.data["income_txn_count"], 0)
        self.assertEqual(response.data["total_txn_count"], 7)
        self.assertEqual(response.data["spending_expense_txn_count"], 3)

    def test_insights_rejects_unknown_period_filters(self):
        response = self.client.get("/api/dashboard/insights/?period=forever")

        self.assertEqual(response.status_code, 400)

    def test_statement_detail_uses_decimal_flow_and_separates_years_in_daily_totals(self):
        statement = Statement.objects.create(user=self.user, filename="multi-year.csv")
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2025, 10, 12),
            amount=Decimal("100.10"),
            merchant_name="Employer",
            raw_description="Credit",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 12),
            amount=Decimal("-0.10"),
            merchant_name="Cafe",
            raw_description="Debit",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 10, 12),
            amount=Decimal("-50.00"),
            merchant_name="Own Account",
            raw_description="Internal transfer",
            is_transfer=True,
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2027, 10, 12),
            amount=Decimal("900000.00"),
            merchant_name="Pending Credit",
            raw_description="Pending",
            is_pending=True,
        )

        response = self.client.get(f"/api/analysis/statements/{statement.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stats"]["total_credits"], 100.10)
        self.assertEqual(response.data["stats"]["total_expenses"], 50.10)
        self.assertEqual(response.data["stats"]["net"], 50.00)
        self.assertEqual(
            response.data["daily_data"],
            [
                {"date": "Oct 12, 2025", "credits": 100.10, "expenses": 0.00},
                {"date": "Oct 12, 2026", "credits": 0.00, "expenses": 50.10},
            ],
        )
        self.assertEqual(response.data["transactions"][-1]["cumulative_flow"], 50.00)

    def test_cash_flow_keeps_calendar_days_from_different_years_separate(self):
        Transaction.objects.create(
            user=self.user,
            date=date(2025, 10, 12),
            amount=Decimal("100.00"),
            raw_description="Credit",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 12),
            amount=Decimal("-50.00"),
            raw_description="Debit",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 12),
            amount=Decimal("-500.00"),
            raw_description="Pending debit",
            is_pending=True,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2025, 10, 12),
            amount=Decimal("-25.00"),
            raw_description="Transfer",
            is_transfer=True,
        )

        response = self.client.get("/api/dashboard/cash-flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            [
                {"date": "2025-10-12", "income": 100.00, "expenses": 25.00},
                {"date": "2026-10-12", "income": 0.00, "expenses": 50.00},
            ],
        )

    @patch("dashboard.views_insights.timezone.localdate", return_value=date(2026, 3, 15))
    def test_monthly_comparison_includes_transfers_but_categories_exclude_them(self, _today):
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
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 3, 14),
            amount=Decimal("-20000.00"),
            merchant_name="Pending purchase",
            raw_description="Pending purchase",
            is_pending=True,
        )

        response = self.client.get("/api/dashboard/monthly-comparison/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["current_period"], {"start": "2026-03-01", "end": "2026-03-15"})
        self.assertEqual(response.data["previous_period"], {"start": "2026-02-01", "end": "2026-02-15"})
        self.assertEqual(response.data["expenses"]["current"], 10499.0)
        self.assertEqual(response.data["expenses"]["previous"], 300.0)
        self.assertEqual(response.data["expenses"]["change"]["amount"], 10199.0)
        self.assertEqual(response.data["categories"][0]["category"], "Comparison dining")
        self.assertEqual(response.data["categories"][0]["current"], 500.0)
        self.assertEqual(response.data["transaction_counts"], {
            "debits": {"current": 2, "previous": 1},
            "credits": {"current": 0, "previous": 0},
        })

    def test_monthly_trend_includes_transfers_and_excludes_pending_transactions(self):
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 2),
            amount=Decimal("-100.00"),
            raw_description="Purchase",
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 2),
            amount=Decimal("-50.00"),
            raw_description="Transfer",
            is_transfer=True,
        )
        Transaction.objects.create(
            user=self.user,
            date=date(2026, 10, 3),
            amount=Decimal("-900.00"),
            raw_description="Pending purchase",
            is_pending=True,
        )

        response = self.client.get("/api/dashboard/monthly-trend/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["expenses"], 150.00)
        self.assertEqual(response.data[0]["net"], -150.00)

    @patch("dashboard.views.timezone.localdate", return_value=date(2026, 10, 15))
    def test_overview_groups_balances_and_cash_flow_by_currency_and_reports_attention(self, _today):
        usd_account = Account.objects.create(user=self.user, name="USD checking")
        eur_account = Account.objects.create(user=self.user, name="EUR checking")
        usd_statement = Statement.objects.create(
            user=self.user,
            filename="usd.csv",
            summary_data={"currency": "USD", "closing_balance": "1000.00"},
        )
        eur_statement = Statement.objects.create(
            user=self.user,
            filename="eur.csv",
            summary_data={"currency": "EUR", "closing_balance": "900.00"},
        )
        Transaction.objects.create(
            user=self.user,
            account=usd_account,
            statement=usd_statement,
            date=date(2026, 10, 2),
            amount=Decimal("100.00"),
            currency="USD",
            raw_description="Salary",
        )
        Transaction.objects.create(
            user=self.user,
            account=usd_account,
            statement=usd_statement,
            date=date(2026, 10, 3),
            amount=Decimal("-20.00"),
            currency="USD",
            raw_description="Purchase",
        )
        Transaction.objects.create(
            user=self.user,
            account=usd_account,
            statement=usd_statement,
            date=date(2026, 9, 3),
            amount=Decimal("50.00"),
            currency="USD",
            raw_description="Previous salary",
        )
        Transaction.objects.create(
            user=self.user,
            account=eur_account,
            statement=eur_statement,
            date=date(2026, 10, 4),
            amount=Decimal("200.00"),
            currency="EUR",
            raw_description="Salary",
        )
        StatementImportAttempt.objects.create(
            user=self.user,
            filename="broken.csv",
            status="failed",
            detail="Invalid statement",
        )

        response = self.client.get("/api/dashboard/overview/?period=month")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["period"]["start"], "2026-10-01")
        flows = {row["currency"]: row for row in response.data["cash_flow_by_currency"]}
        self.assertEqual(flows["USD"]["income"], "100.00")
        self.assertEqual(flows["USD"]["spending"], "20.00")
        self.assertEqual(flows["USD"]["previous_net"], "50.00")
        self.assertEqual(flows["EUR"]["income"], "200.00")
        balances = {row["currency"]: row["amount"] for row in response.data["current_balances"]}
        self.assertEqual(balances, {"EUR": "900.00", "USD": "1000.00"})
        self.assertEqual(response.data["attention_needed"]["failed_imports"], 1)
        self.assertEqual(response.data["attention_needed"]["uncategorized_transactions"], 4)
