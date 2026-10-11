"""Small read-only HTTP client for the authenticated Spendstack REST API."""

import httpx


class SpendstackAPIError(RuntimeError):
    """A safe, user-facing API or transport error."""


class SpendstackAPI:
    def __init__(self, api_url, api_token, timeout_seconds=15.0, transport=None):
        self._base_url = api_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "User-Agent": "spendstack-mcp/1.0",
            },
        )

    def close(self):
        self._client.close()

    def get(self, path, params=None):
        try:
            response = self._client.get(f"{self._base_url}{path}", params=params)
        except httpx.TimeoutException:
            raise SpendstackAPIError("Spendstack API request timed out.") from None
        except httpx.RequestError:
            raise SpendstackAPIError("Could not connect to the Spendstack API.") from None

        if response.is_redirect:
            raise SpendstackAPIError("Spendstack API returned an unexpected redirect; request was not followed.")
        if response.status_code == 401:
            raise SpendstackAPIError("Spendstack API rejected the configured token (HTTP 401). Check SPENDSTACK_API_TOKEN.")
        if response.status_code == 403:
            raise SpendstackAPIError("Spendstack API denied access (HTTP 403).")
        if response.status_code == 404:
            raise SpendstackAPIError("Spendstack API resource was not found (HTTP 404).")
        if not response.is_success:
            raise SpendstackAPIError(f"Spendstack API returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise SpendstackAPIError("Spendstack API returned an invalid JSON response.") from None