from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Category, SpendingAlert, Transaction, TransactionRule
from .services import classify_transaction, detect_recurring_bills, is_transfer_description


class TransactionClassificationTests(TestCase):
    def test_transfer_descriptions_are_detected_but_transfer_fees_are_not(self):
        self.assertTrue(is_transfer_description("NIP/TRF/John Doe/REF123"))
        self.assertTrue(is_transfer_description("NIP/TRANSFER/John Doe/REF123"))
        self.assertTrue(is_transfer_description("Mobile transfer to savings"))
        self.assertFalse(is_transfer_description("NIP transfer charge and VAT"))
        self.assertFalse(is_transfer_description("POS purchase at Transfer Market"))

    def test_custom_rules_rename_categorize_and_override_transfer_detection(self):
        rule = TransactionRule(
            match_text="netflix",
            match_field=TransactionRule.MATCH_EITHER,
            merchant_name="Netflix",
            category_name="Entertainment",
        )
        self.assertEqual(
            classify_transaction("CARD PAYMENT NETFLIX", "NETFLIX.COM", "Other", [rule]),
            ("Netflix", "Entertainment", False),
        )

        transfer_rule = TransactionRule(
            match_text="own savings",
            transfer_action=TransactionRule.TRANSFER_YES,
        )
        self.assertEqual(
            classify_transaction("Monthly move to own savings", "Savings", "Income", [transfer_rule]),
            ("Savings", "Transfers", True),
        )

    def test_recurring_detection_requires_three_consistent_non_transfer_expenses(self):
        category = Category.objects.create(name="Utilities")
        user = get_user_model().objects.create_user(email="bills@example.com", password="Strong-password-123")
        today = date(2026, 10, 1)
        for offset in (60, 30, 0):
            Transaction.objects.create(
                user=user,
                date=today - timedelta(days=offset),
                amount=Decimal("-12500.00"),
                merchant_name="Acme Power",
                raw_description="Electric utility bill",
                category=category,
            )
        Transaction.objects.create(
            user=user,
            date=today,
            amount=Decimal("-12500.00"),
            merchant_name="Internal Transfer",
            raw_description="Internal transfer",
            is_transfer=True,
        )

        bills = detect_recurring_bills(
            Transaction.objects.filter(user=user).select_related("category")
        )
        self.assertEqual(len(bills), 1)
        self.assertEqual(bills[0]["merchant"], "Acme Power")
        self.assertEqual(bills[0]["cadence"], "monthly")
        self.assertEqual(bills[0]["occurrences"], 3)


class FinanceToolsApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="owner@example.com",
            password="Strong-password-123",
        )
        self.other_user = get_user_model().objects.create_user(
            email="other@example.com",
            password="Strong-password-123",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_spending_alert_reports_current_spend_excluding_transfers(self):
        category = Category.objects.create(name="Dining")
        today = date.today()
        Transaction.objects.create(
            user=self.user,
            date=today,
            amount=Decimal("-7000.00"),
            raw_description="Restaurant",
            merchant_name="Restaurant",
            category=category,
        )
        Transaction.objects.create(
            user=self.user,
            date=today,
            amount=Decimal("-5000.00"),
            raw_description="Transfer to savings",
            merchant_name="Savings",
            is_transfer=True,
        )
        response = self.client.post(
            "/api/transactions/alerts/",
            {
                "category_name": "Dining",
                "threshold": "6000.00",
                "period": SpendingAlert.MONTHLY,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Decimal(response.data["current_spend"]), Decimal("7000.00"))
        self.assertTrue(response.data["is_exceeded"])

    def test_rule_changes_only_apply_to_own_users_transactions_and_delete_is_scoped(self):
        category = Category.objects.create(name="Other")
        own_transaction = Transaction.objects.create(
            user=self.user,
            date=date.today(),
            amount=Decimal("-100.00"),
            raw_description="Coffee at Green Cafe",
            merchant_name="Green Cafe",
            original_merchant_name="Green Cafe",
            original_category_name="Other",
            category=category,
        )
        other_transaction = Transaction.objects.create(
            user=self.other_user,
            date=date.today(),
            amount=Decimal("-100.00"),
            raw_description="Coffee at Green Cafe",
            merchant_name="Green Cafe",
            original_merchant_name="Green Cafe",
            original_category_name="Other",
            category=category,
        )
        response = self.client.post(
            "/api/transactions/rules/",
            {
                "match_text": "green cafe",
                "match_field": "either",
                "merchant_name": "Green Cafe Coffee",
                "category_name": "Dining",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        own_transaction.refresh_from_db()
        other_transaction.refresh_from_db()
        self.assertEqual(own_transaction.merchant_name, "Green Cafe Coffee")
        self.assertEqual(own_transaction.category.name, "Dining")
        self.assertEqual(other_transaction.merchant_name, "Green Cafe")
        self.assertEqual(
            self.client.delete(f"/api/transactions/rules/{response.data['id'] + 1}/").status_code,
            404,
        )

    def test_rules_revert_transactions_to_original_classification_when_deleted(self):
        category = Category.objects.create(name="Other")
        target = Transaction.objects.create(
            user=self.user,
            date=date.today(),
            amount=Decimal("-100.00"),
            raw_description="Coffee at Green Cafe",
            merchant_name="Green Cafe Coffee",
            original_merchant_name="Green Cafe",
            original_category_name="Other",
            category=category,
        )
        rule = TransactionRule.objects.create(
            user=self.user,
            match_text="green cafe",
            match_field=TransactionRule.MATCH_EITHER,
            merchant_name="Green Cafe Coffee",
            category_name="Dining",
        )
        response = self.client.delete(f"/api/transactions/rules/{rule.pk}/")
        self.assertEqual(response.status_code, 204)
        target.refresh_from_db()
        self.assertEqual(target.merchant_name, "Green Cafe")
        self.assertEqual(target.category.name, "Other")
