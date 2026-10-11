import unittest

import httpx

from spendstack_mcp.api import SpendstackAPI, SpendstackAPIError
from spendstack_mcp.config import ConfigurationError, load_settings


class SettingsTests(unittest.TestCase):
    def test_loads_https_configuration(self):
        settings = load_settings({
            "SPENDSTACK_API_URL": "https://api.example.test/",
            "SPENDSTACK_API_TOKEN": "opaque-token",
        })
        self.assertEqual(settings.api_url, "https://api.example.test")
        self.assertEqual(settings.api_token, "opaque-token")

    def test_allows_http_only_for_loopback(self):
        self.assertEqual(
            load_settings({"SPENDSTACK_API_URL": "http://localhost:8000", "SPENDSTACK_API_TOKEN": "t"}).api_url,
            "http://localhost:8000",
        )
        with self.assertRaises(ConfigurationError):
            load_settings({"SPENDSTACK_API_URL": "http://api.example.test", "SPENDSTACK_API_TOKEN": "t"})

    def test_rejects_missing_or_unsafe_configuration_without_echoing_token(self):
        for env in (
            {"SPENDSTACK_API_TOKEN": "sensitive-token"},
            {"SPENDSTACK_API_URL": "https://api.example.test"},
            {"SPENDSTACK_API_URL": "https://user:pass@example.test", "SPENDSTACK_API_TOKEN": "sensitive-token"},
            {"SPENDSTACK_API_URL": "https://api.example.test/?key=x", "SPENDSTACK_API_TOKEN": "sensitive-token"},
        ):
            with self.subTest(env=env), self.assertRaises(ConfigurationError) as error:
                load_settings(env)
            self.assertNotIn("sensitive-token", str(error.exception))


class APIClientTests(unittest.TestCase):
    def make_client(self, handler):
        transport = httpx.MockTransport(handler)
        return SpendstackAPI("https://api.example.test", "private-token", transport=transport)

    def test_get_uses_bearer_token_query_and_returns_json(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/transactions/")
            self.assertEqual(request.url.params["statement_id"], "17")
            self.assertEqual(request.headers["Authorization"], "Bearer private-token")
            return httpx.Response(200, json=[{"id": 1, "amount": -4.5}])

        client = self.make_client(handler)
        try:
            self.assertEqual(client.get("/api/transactions/", {"statement_id": 17}), [{"id": 1, "amount": -4.5}])
        finally:
            client.close()

    def test_http_errors_are_explicit_and_do_not_echo_body_or_token(self):
        client = self.make_client(lambda request: httpx.Response(
            401, json={"detail": "private-token should not appear", "debug": "secret"}
        ))
        try:
            with self.assertRaises(SpendstackAPIError) as error:
                client.get("/api/savings/wallet/")
            self.assertIn("HTTP 401", str(error.exception))
            self.assertNotIn("private-token", str(error.exception))
            self.assertNotIn("secret", str(error.exception))
        finally:
            client.close()

    def test_refuses_redirects_and_invalid_json(self):
        for response, expected in (
            (httpx.Response(302, headers={"Location": "https://elsewhere.example"}), "redirect"),
            (httpx.Response(200, text="not json"), "invalid JSON"),
        ):
            client = self.make_client(lambda request, response=response: response)
            try:
                with self.assertRaises(SpendstackAPIError) as error:
                    client.get("/api/savings/wallet/")
                self.assertIn(expected, str(error.exception))
            finally:
                client.close()

    def test_transport_error_is_sanitized(self):
        def fail(request):
            raise httpx.ConnectError("Bearer private-token", request=request)

        client = self.make_client(fail)
        try:
            with self.assertRaises(SpendstackAPIError) as error:
                client.get("/api/savings/wallet/")
            self.assertIn("Could not connect", str(error.exception))
            self.assertNotIn("private-token", str(error.exception))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()