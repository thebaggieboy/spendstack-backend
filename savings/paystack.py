import os

import requests


PAYSTACK_API = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def paystack_secret_key():
    secret_key = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    return secret_key or os.environ.get("NEXT_PAYSTACK_SECRET_KEY", "").strip()


def paystack_request(method, path, *, params=None, payload=None):
    secret_key = paystack_secret_key()
    if not secret_key:
        raise PaystackError("Paystack is not configured on the server.")

    try:
        response = requests.request(
            method,
            f"{PAYSTACK_API}{path}",
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
            },
            params=params,
            json=payload,
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        raise PaystackError("Could not reach Paystack. Check the transaction status before retrying.") from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise PaystackError("Paystack returned an invalid response.") from exc

    if not isinstance(result, dict):
        raise PaystackError("Paystack returned an invalid response.")
    if not response.ok or not result.get("status"):
        message = result.get("message")
        if response.status_code in (401, 403):
            raise PaystackError("Paystack rejected the server credentials or account permissions.")
        if response.status_code >= 500:
            raise PaystackError("Paystack is temporarily unavailable.")
        raise PaystackError(message if isinstance(message, str) else "Paystack could not process the request.")
    data = result.get("data")
    if not isinstance(data, (dict, list)):
        raise PaystackError("Paystack returned an incomplete response.")
    return data
