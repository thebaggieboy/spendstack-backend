import hashlib
import hmac
import os
import time
from urllib.parse import urlparse

import requests


class BachsError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def _configuration():
    api_key = os.environ.get("BACHS_API_KEY", "").strip()
    if api_key.startswith("sk_sandbox_"):
        expected_url = "https://sandbox-api.bachs.io"
    elif api_key.startswith("sk_live_"):
        expected_url = "https://api.bachs.io"
    else:
        raise BachsError("Bachs is not configured. Add a sandbox or live Bachs API key on the server.")
    gateway_url = os.environ.get("GATEWAY_URL", expected_url).strip().rstrip("/")
    if gateway_url != expected_url:
        raise BachsError("GATEWAY_URL must match the environment indicated by the Bachs API key.")
    return api_key, gateway_url


def _bachs_request(method, path, *, payload=None, idempotency_key=None, account_id=None):
    api_key, api_url = _configuration()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if account_id:
        headers["X-Account-Id"] = account_id
    try:
        response = requests.request(
            method,
            f"{api_url}{path}",
            headers=headers,
            json=payload,
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        raise BachsError("Could not reach Bachs. Check the operation status before retrying.", uncertain=True) from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise BachsError("Bachs returned an invalid response.", uncertain=response.status_code >= 500) from exc
    if not isinstance(result, dict):
        raise BachsError("Bachs returned an invalid response.", uncertain=response.status_code >= 500)
    if not response.ok:
        detail = result.get("detail") or result.get("message")
        if response.status_code in (401, 403):
            raise BachsError("Bachs rejected the API key or its permissions.")
        if response.status_code >= 500:
            raise BachsError("Bachs is temporarily unavailable.", uncertain=True)
        raise BachsError(detail if isinstance(detail, str) else "Bachs could not process the request.")
    data = result.get("data", result)
    if not isinstance(data, dict):
        raise BachsError("Bachs returned an incomplete response.")
    return data


def create_checkout_session(*, amount, email, reference, success_url, cancel_url, metadata):
    api_key, api_url = _configuration()
    try:
        response = requests.post(
            f"{api_url}/v1/checkout-sessions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Idempotency-Key": reference,
            },
            json={
                "pricing": {"currency": "NGN", "amount": amount},
                "customer": {"email": email},
                "reference": reference,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata,
            },
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        raise BachsError(
            "Could not reach Bachs. Check the checkout status before trying again.",
            uncertain=True,
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise BachsError("Bachs returned an invalid response.", uncertain=True) from exc

    if not isinstance(result, dict):
        raise BachsError("Bachs returned an invalid response.", uncertain=True)
    if not response.ok:
        detail = result.get("detail")
        if response.status_code in (401, 403):
            raise BachsError("Bachs rejected the API key or its payment permissions.")
        if response.status_code >= 500:
            raise BachsError("Bachs is temporarily unavailable.", uncertain=True)
        raise BachsError(detail if isinstance(detail, str) else "Bachs could not create the checkout.")

    checkout_url = result.get("checkout_url")
    checkout_id = result.get("checkout_id")
    try:
        checkout_host = urlparse(checkout_url).hostname if isinstance(checkout_url, str) else None
    except ValueError:
        checkout_host = None
    if (
        not isinstance(checkout_url, str)
        or not checkout_url.startswith("https://")
        or checkout_host != "checkout.bachs.io"
        or not isinstance(checkout_id, str)
        or not checkout_id.startswith("chk_")
    ):
        raise BachsError("Bachs did not return a valid hosted checkout session.", uncertain=True)
    return {"checkout_url": checkout_url, "checkout_id": checkout_id}


def create_payout_destination(*, bank_code, account_number):
    result = _bachs_request(
        "POST",
        "/v1/payouts/destinations",
        payload={
            "type": "bank_account",
            "currency": "NGN",
            "bank_code": bank_code,
            "account_number": account_number,
        },
    )
    destination_id = result.get("id")
    account_name = result.get("account_name")
    if not isinstance(destination_id, str) or not destination_id.startswith("pd_"):
        raise BachsError("Bachs did not return a valid payout destination.")
    if not isinstance(account_name, str) or not account_name.strip():
        raise BachsError("Bachs could not verify the bank account name.")
    if not isinstance(result.get("is_usable"), bool):
        raise BachsError("Bachs returned an incomplete payout destination.")
    return {
        "id": destination_id,
        "account_name": account_name.strip(),
        "is_usable": result["is_usable"],
    }


def get_payout_destination(destination_id):
    result = _bachs_request("GET", f"/v1/payouts/destinations/{destination_id}")
    if result.get("id") != destination_id or not isinstance(result.get("is_usable"), bool):
        raise BachsError("Bachs returned an invalid payout destination.")
    return result


def create_payout(*, amount, destination_id, reference):
    return _bachs_request(
        "POST",
        "/v1/payouts",
        idempotency_key=reference,
        account_id=os.environ.get("BACHS_ACCOUNT_ID", "").strip() or None,
        payload={"destination": destination_id, "amount": amount, "reference": reference},
    )


def verify_webhook_signature(header, raw_body, secret, *, now=None, tolerance=300):
    if not header or not secret:
        return False
    parts = [part.strip().split("=", 1) for part in header.split(",") if "=" in part]
    timestamp_values = [value for key, value in parts if key == "t"]
    signatures = [value for key, value in parts if key == "v1"]
    if len(timestamp_values) != 1 or not signatures:
        return False
    try:
        timestamp = int(timestamp_values[0])
    except ValueError:
        return False
    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp) > tolerance:
        return False
    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)
