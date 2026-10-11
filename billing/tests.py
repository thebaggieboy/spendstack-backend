import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CheckoutAttempt, Subscription
from .permissions import user_has_pro
from .services import BillingError, configured_plan, process_subscription_event


class BillingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="billing-user@example.com",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.plan = {
            "plan_code": "PLN_spendstack_pro",
            "name": "Spendstack Pro",
            "amount": 250000,
            "currency": "NGN",
            "interval": "monthly",
        }

    @patch("billing.views.configured_plan")
    @patch("billing.views.validate_token_encryption")
    @patch("billing.views.paystack_request")
    def test_checkout_uses_configured_plan_and_never_credits_on_initialize(
        self,
        paystack_request,
        validate_token_encryption,
        configured_plan,
    ):
        configured_plan.return_value = self.plan
        paystack_request.return_value = {
            "authorization_url": "https://checkout.paystack.com/test",
        }

        response = self.client.post("/api/billing/checkout/", {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        paystack_request.assert_called_once()
        payload = paystack_request.call_args.kwargs["payload"]
        self.assertEqual(payload["plan"], self.plan["plan_code"])
        self.assertEqual(payload["email"], self.user.email)
        self.assertNotIn("amount", payload)
        self.assertEqual(CheckoutAttempt.objects.get().status, CheckoutAttempt.PENDING)
        self.assertFalse(Subscription.objects.exists())

    @patch("billing.services.paystack_request")
    def test_rejects_non_monthly_or_non_ngn_plan(self, paystack_request):
        paystack_request.return_value = {**self.plan, "currency": "USD"}
        with patch.dict("os.environ", {"PAYSTACK_PRO_PLAN_CODE": self.plan["plan_code"]}):
            with self.assertRaises(BillingError):
                configured_plan()

    def test_signed_subscription_webhook_activates_only_matching_checkout(self):
        attempt = CheckoutAttempt.objects.create(
            user=self.user,
            reference="SS-PRO-test",
            plan_code=self.plan["plan_code"],
            amount_kobo=self.plan["amount"],
        )
        event = {
            "event": "subscription.create",
            "data": {
                "plan": {"plan_code": self.plan["plan_code"]},
                "subscription_code": "SUB_test",
                "email_token": "sensitive-token",
                "status": "active",
                "amount": self.plan["amount"],
                "next_payment_date": (timezone.now() + timedelta(days=30)).isoformat(),
                "customer": {"email": self.user.email},
            },
        }
        body = json.dumps(event).encode()
        signature = hmac.new(b"billing-test-secret", body, hashlib.sha512).hexdigest()

        with patch.dict("os.environ", {
            "PAYSTACK_SECRET_KEY": "billing-test-secret",
            "PAYSTACK_PRO_PLAN_CODE": self.plan["plan_code"],
            "BILLING_TOKEN_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        }):
            response = self.client.generic(
                "POST",
                "/api/savings/webhooks/paystack/",
                body,
                content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=signature,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempt.__class__.objects.get(pk=attempt.pk).status, CheckoutAttempt.COMPLETED)
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, Subscription.ACTIVE)
        self.assertTrue(user_has_pro(self.user))
        self.assertNotEqual(subscription.encrypted_email_token, "sensitive-token")
        subscription.status = Subscription.CANCELLED
        subscription.save(update_fields=["status"])
        self.assertFalse(
            process_subscription_event("subscription.create", event["data"])
        )

    def test_free_user_cannot_access_pro_feature_or_checkout_for_someone_else(self):
        response = self.client.post(
            "/api/dashboard/chat/",
            {"question": "How much did I spend?"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("upgrade_required", str(response.data))

        other_client = APIClient()
        response = other_client.get(
            "/api/billing/checkout/verify/?reference=not-owned",
        )
        self.assertEqual(response.status_code, 401)

    def test_cancelled_subscription_keeps_access_only_until_paid_period_end(self):
        subscription = Subscription.objects.create(
            user=self.user,
            plan_code=self.plan["plan_code"],
            subscription_code="SUB_cancelled",
            encrypted_email_token="encrypted",
            amount_kobo=self.plan["amount"],
            next_payment_date=timezone.now() + timedelta(days=1),
            cancel_at_period_end=True,
        )
        self.assertTrue(subscription.has_access())
        subscription.next_payment_date = timezone.now() - timedelta(seconds=1)
        subscription.save(update_fields=["next_payment_date"])
        self.assertFalse(subscription.has_access())

    def test_free_user_can_read_but_cannot_create_alerts_or_transaction_rules(self):
        response = self.client.get("/api/transactions/alerts/")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/transactions/alerts/",
            {"category_name": "", "threshold": "5000", "period": "monthly"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/transactions/rules/",
            {"match_text": "TEST", "match_field": "either"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    @patch("billing.views.paystack_request")
    def test_cancel_calls_paystack_before_setting_renewal_cancelled(self, paystack_request):
        subscription = Subscription.objects.create(
            user=self.user,
            plan_code=self.plan["plan_code"],
            subscription_code="SUB_cancel",
            encrypted_email_token="encrypted",
            amount_kobo=self.plan["amount"],
            next_payment_date=timezone.now() + timedelta(days=30),
        )
        with patch("billing.views.decrypt_email_token", return_value="email-token"):
            paystack_request.return_value = {}
            response = self.client.post("/api/billing/subscription/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        paystack_request.assert_called_once_with(
            "POST",
            "/subscription/disable",
            payload={"code": "SUB_cancel", "token": "email-token"},
        )
        subscription.refresh_from_db()
        self.assertTrue(subscription.cancel_at_period_end)
