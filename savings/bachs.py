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
        return api_key, "https://sandbox-api.bachs.io"
    if api_key.startswith("sk_live_"):
        return api_key, "https://api.bachs.io"
    raise BachsError("Bachs is not configured. Add a sandbox or live Bachs API key on the server.")


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
