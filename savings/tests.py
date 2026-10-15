import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import BankAccount, LockPeriod, Schedule, Wallet, WalletTransaction
from .bachs import verify_webhook_signature
from .paystack import paystack_request
from .services import credit_verified_funding, finalize_payout_attempt, to_kobo


class SavingsApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="savings-owner@example.com",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.bank_account = BankAccount.objects.create(
            user=self.user,
            bank_name="Example Bank",
            bank_code="001",
            account_name="Savings Owner",
            account_last4="1234",
            recipient_code="RCP_test",
        )

    def test_lock_reserves_available_funds_and_creates_full_payout_schedule(self):
        Wallet.objects.create(
            user=self.user,
            available_balance=Decimal("50000.00"),
            locked_balance=Decimal("1000.00"),
        )

        response = self.client.post(
            "/api/savings/locks/",
            {
                "amount": "12000.00",
                "interval_days": 30,
                "description": "Rent reserve",
                "bank_account_id": str(self.bank_account.id),
                "auto_payout": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.available_balance, Decimal("38000.00"))
        self.assertEqual(wallet.locked_balance, Decimal("13000.00"))
        lock = LockPeriod.objects.get(user=self.user)
        schedule = Schedule.objects.get(lock_period=lock)
        self.assertEqual(schedule.payout_amount, lock.amount)
        self.assertEqual(schedule.bank_account_id, self.bank_account.id)

    def test_lock_rejects_insufficient_available_balance(self):
        Wallet.objects.create(user=self.user, available_balance=Decimal("20.00"))
        response = self.client.post(
            "/api/savings/locks/",
            {
                "amount": "21.00",
                "interval_days": 14,
                "bank_account_id": str(self.bank_account.id),
                "auto_payout": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LockPeriod.objects.exists())

    def test_wallet_credits_only_matching_verified_paystack_payment_and_is_idempotent(self):
        pending = WalletTransaction.objects.create(
            user=self.user,
            type=WalletTransaction.DEPOSIT,
            amount=Decimal("2500.00"),
            balance_before=Decimal("0.00"),
            balance_after=Decimal("0.00"),
            status=WalletTransaction.PENDING,
            reference="SS-FUND-test",
        )
        verified = {
            "status": "success",
            "currency": "NGN",
            "amount": 250000,
            "reference": pending.reference,
            "customer": {"email": self.user.email},
        }

        self.assertTrue(credit_verified_funding(pending.reference, verified))
        self.assertTrue(credit_verified_funding(pending.reference, verified))

        self.assertEqual(Wallet.objects.get(user=self.user).available_balance, Decimal("2500.00"))
        self.assertEqual(
            WalletTransaction.objects.get(pk=pending.pk).status,
            WalletTransaction.COMPLETED,
        )

    def test_transfer_webhooks_finalize_and_restore_reversed_payouts(self):
        wallet = Wallet.objects.create(
            user=self.user,
            available_balance=Decimal("0.00"),
            locked_balance=Decimal("10000.00"),
        )
        lock = LockPeriod.objects.create(
            user=self.user,
            amount=Decimal("10000.00"),
            lock_date=timezone.now() - timedelta(days=15),
            unlock_date=timezone.now() - timedelta(days=1),
            interval_days=14,
            status=LockPeriod.PENDING,
            bank_account=self.bank_account,
        )
        schedule = Schedule.objects.create(
            user=self.user,
            lock_period=lock,
            bank_account=self.bank_account,
            title="Maturity payout",
            amount=Decimal("10000.00"),
            payout_amount=Decimal("10000.00"),
            scheduled_date=timezone.now() - timedelta(days=1),
            status=Schedule.PROCESSING,
            reference="SS-PAYOUT-test",
        )
        from .models import PayoutAttempt

        PayoutAttempt.objects.create(
            schedule=schedule,
            reference=schedule.reference,
            amount=schedule.payout_amount,
            status=Schedule.PROCESSING,
        )
        event_data = {
            "reference": schedule.reference,
            "amount": to_kobo(schedule.payout_amount),
        }

        self.assertFalse(
            finalize_payout_attempt(schedule.reference, "transfer.success", {**event_data, "amount": 1})
        )
        self.assertTrue(finalize_payout_attempt(schedule.reference, "transfer.success", event_data))
        self.assertTrue(finalize_payout_attempt(schedule.reference, "transfer.success", event_data))

        wallet.refresh_from_db()
        lock.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(wallet.locked_balance, Decimal("0.00"))
        self.assertEqual(lock.status, LockPeriod.UNLOCKED)
        self.assertEqual(schedule.status, Schedule.COMPLETED)

        self.assertTrue(finalize_payout_attempt(schedule.reference, "transfer.reversed", event_data))
        self.assertTrue(finalize_payout_attempt(schedule.reference, "transfer.reversed", event_data))
        wallet.refresh_from_db()
        lock.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(wallet.locked_balance, Decimal("10000.00"))
        self.assertEqual(lock.status, LockPeriod.PENDING)
        self.assertEqual(schedule.status, Schedule.FAILED)
        self.assertEqual(WalletTransaction.objects.filter(type="reversal").count(), 1)

    @patch("savings.views.paystack_request")
    def test_funding_initialization_never_credits_wallet(self, paystack_request):
        paystack_request.return_value = {"authorization_url": "https://checkout.paystack.com/example"}
        response = self.client.post(
            "/api/savings/fund/initialize/",
            {"amount": "1000"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Wallet.objects.get(user=self.user).available_balance, Decimal("0.00"))
        self.assertEqual(
            WalletTransaction.objects.get(reference=response.data["reference"]).status,
            WalletTransaction.PENDING,
        )

    @patch("savings.views.paystack_request")
    def test_funding_initialization_rejects_invalid_checkout_url(self, paystack_request):
        paystack_request.return_value = {"authorization_url": "https://example.com/fake-checkout"}
        response = self.client.post(
            "/api/savings/fund/initialize/",
            {"amount": "1000"},
            format="json",
        )
        self.assertEqual(response.status_code, 502)
        transaction = WalletTransaction.objects.get(type=WalletTransaction.DEPOSIT)
        self.assertEqual(transaction.status, WalletTransaction.FAILED)

    @patch.dict("os.environ", {"BACHS_API_KEY": "sk_sandbox_test"})
    @patch("savings.views.create_checkout_session")
    def test_bachs_funding_initialization_creates_ngn_checkout_without_crediting_wallet(self, create_checkout):
        create_checkout.return_value = {
            "checkout_url": "https://checkout.bachs.io/c/test-session",
            "checkout_id": "chk_test",
        }
        response = self.client.post(
            "/api/savings/fund/initialize/",
            {"amount": "1500.00", "gateway": "bachs"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["gateway"], "bachs")
        self.assertIn("funding=bachs-return", create_checkout.call_args.kwargs["success_url"])
        self.assertEqual(create_checkout.call_args.kwargs["amount"], "1500.00")
        wallet_transaction = WalletTransaction.objects.get(reference=response.data["reference"])
        self.assertEqual(wallet_transaction.status, WalletTransaction.PENDING)
        self.assertEqual(wallet_transaction.metadata["payment_provider"], "bachs")
        self.assertEqual(wallet_transaction.metadata["checkout_id"], "chk_test")
        self.assertEqual(Wallet.objects.get(user=self.user).available_balance, Decimal("0.00"))

    @patch.dict("os.environ", {"BACHS_WEBHOOK_SECRET": "test-bachs-secret"})
    def test_signed_bachs_collection_webhook_credits_matching_funding_once(self):
        pending = WalletTransaction.objects.create(
            user=self.user,
            type=WalletTransaction.DEPOSIT,
            amount=Decimal("2500.00"),
            balance_before=Decimal("0.00"),
            balance_after=Decimal("0.00"),
            status=WalletTransaction.PENDING,
            reference="SS-FUND-BACHS-test",
            metadata={"payment_provider": "bachs", "checkout_id": "chk_test"},
        )
        event = {
            "id": "evt_test",
            "type": "collection.succeeded",
            "data": {
                "reference": pending.reference,
                "checkout_id": "chk_test",
                "status": "SUCCEEDED",
                "amount": "2500.00",
                "currency": "NGN",
            },
        }
        raw_body = json.dumps(event, separators=(",", ":")).encode()
        timestamp = int(time.time())
        digest = hmac.new(
            b"test-bachs-secret",
            f"{timestamp}.".encode() + raw_body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"t={timestamp},v1={digest}"

        response = self.client.post(
            "/api/savings/webhooks/bachs/",
            data=raw_body,
            content_type="application/json",
            HTTP_X_BACHS_SIGNATURE_V2=signature,
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/savings/webhooks/bachs/",
            data=raw_body,
            content_type="application/json",
            HTTP_X_BACHS_SIGNATURE_V2=signature,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Wallet.objects.get(user=self.user).available_balance, Decimal("2500.00"))
        pending.refresh_from_db()
        self.assertEqual(pending.status, WalletTransaction.COMPLETED)

    def test_bachs_webhook_signature_rejects_stale_or_invalid_signatures(self):
        raw_body = b'{"type":"collection.succeeded"}'
        timestamp = int(time.time()) - 600
        digest = hmac.new(
            b"test-bachs-secret",
            f"{timestamp}.".encode() + raw_body,
            hashlib.sha256,
        ).hexdigest()
        self.assertFalse(
            verify_webhook_signature(f"t={timestamp},v1={digest}", raw_body, "test-bachs-secret")
        )
        self.assertFalse(
            verify_webhook_signature("t=invalid,v1=not-a-signature", raw_body, "test-bachs-secret")
        )

    @patch.dict(
        "os.environ",
        {"PAYSTACK_SECRET_KEY": "", "NEXT_PAYSTACK_SECRET_KEY": "test-secret"},
    )
    @patch("savings.views.paystack_request")
    def test_signed_webhook_verifies_funding_before_credit(self, paystack_request):
        paystack_request.return_value = {
            "status": "success",
            "currency": "NGN",
            "amount": 50000,
            "reference": "SS-FUND-webhook",
            "customer": {"email": self.user.email},
        }
        WalletTransaction.objects.create(
            user=self.user,
            type=WalletTransaction.DEPOSIT,
            amount=Decimal("500.00"),
            balance_before=Decimal("0.00"),
            balance_after=Decimal("0.00"),
            status=WalletTransaction.PENDING,
            reference="SS-FUND-webhook",
        )
        body = json.dumps({"event": "charge.success", "data": {"reference": "SS-FUND-webhook"}}).encode()
        signature = hmac.new(b"test-secret", body, hashlib.sha512).hexdigest()

        response = self.client.generic(
            "POST",
            "/api/savings/webhooks/paystack/",
            body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Wallet.objects.get(user=self.user).available_balance, Decimal("500.00"))
        paystack_request.assert_called_once_with("GET", "/transaction/verify/SS-FUND-webhook")

    @patch.dict(
        "os.environ",
        {"PAYSTACK_SECRET_KEY": "", "NEXT_PAYSTACK_SECRET_KEY": "test-secret"},
    )
    @patch("savings.paystack.requests.request")
    def test_paystack_client_uses_next_prefixed_server_secret(self, request):
        request.return_value.ok = True
        request.return_value.json.return_value = {"status": True, "data": {"id": 123}}

        result = paystack_request("GET", "/bank")

        self.assertEqual(result, {"id": 123})
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")

    @patch.dict(
        "os.environ",
        {
            "BANK_LIST": "https://api.paystack.co/bank?currency=NGN&enabled_for_verification=true",
            "PAYSTACK_SECRET_KEY": "",
            "NEXT_PAYSTACK_SECRET_KEY": "verifyme-test-secret",
        },
    )
    @patch("savings.views.requests.get")
    def test_bank_directory_uses_paystack_and_normalizes_bank_codes(self, get):
        self.client.force_authenticate(self.user)
        get.return_value.ok = True
        get.return_value.json.return_value = {
            "status": True,
            "data": [
                {"name": "Example Bank", "code": 120001},
                {"name": "Example Bank", "code": "120001"},
                {"name": "Last Bank", "code": "090270"},
            ],
        }

        response = self.client.get("/api/savings/bank-directory/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            [
                {"name": "Example Bank", "code": "120001"},
                {"name": "Last Bank", "code": "090270"},
            ],
        )
        get.assert_called_once_with(
            "https://api.paystack.co/bank?currency=NGN&enabled_for_verification=true",
            headers={"Authorization": "Bearer verifyme-test-secret"},
            timeout=(5, 20),
        )

    @patch.dict(
        "os.environ",
        {"PAYSTACK_SECRET_KEY": "", "NEXT_PAYSTACK_SECRET_KEY": ""},
    )
    def test_bank_directory_requires_paystack_credentials(self):
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/savings/bank-directory/")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Paystack", response.data["detail"])
