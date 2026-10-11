# Spendstack read-only MCP server

This local stdio MCP server exposes read-only Spendstack wallet, transaction, statement, budget, recurring-bill, spending-alert, and analytics data. It calls the existing authenticated Django REST API with the configured user's Spendstack bearer token; it never accesses the database directly. Every tool issues only `GET` requests. There are no funding, transfer, create, update, or delete tools. API authorization continues to scope results to the token owner; statement detail lookup also uses the existing owner-checked endpoint.

## Requirements and setup

Python 3.10+ is required. From the backend directory, create an isolated environment and install the MCP-specific requirements (the main `requirements.txt` is not changed):

```powershell
cd mcp_server
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r ..\requirements-mcp.txt
```

Set configuration in the process environment or your MCP client's secret/input store:

- `SPENDSTACK_API_URL`: API origin, such as `https://spendstack-backend.onrender.com` (do not append `/api`). HTTPS is required except for `localhost`/loopback development hosts.
- `SPENDSTACK_API_TOKEN`: the user's Spendstack API access token, sent as `Authorization: Bearer …`. Use the access token, not a refresh token. Do not put the token in source control, command-line arguments, or this file.

Run from `backend\mcp_server`:

```powershell
$env:SPENDSTACK_API_URL = 'https://spendstack-backend.onrender.com'
$env:SPENDSTACK_API_TOKEN = '<provide securely in your shell or secret manager>'
.\.venv\Scripts\python.exe -m spendstack_mcp.server
```

The server communicates over stdio; start it from an MCP-compatible client rather than a terminal session intended for interactive output. The HTTP client uses a 15-second timeout, does not follow redirects, and emits sanitized transport/HTTP errors without returning response bodies or credentials.

## MCP client configuration example

For VS Code `mcp.json`, set the API origin directly and use a password input for the token (VS Code prompts locally; the credential is not embedded in this configuration). Replace the sample backend folder and interpreter paths with local paths; do not replace the `${input:...}` placeholder with a token.

```json
{
  "inputs": [
    {
      "id": "spendstackApiToken",
      "type": "promptString",
      "description": "Spendstack API access token",
      "password": true
    }
  ],
  "servers": {
    "spendstack": {
      "type": "stdio",
      "command": "C:\\path\\to\\backend\\mcp_server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "spendstack_mcp.server"],
      "cwd": "C:\\path\\to\\backend\\mcp_server",
      "env": {
        "SPENDSTACK_API_URL": "https://spendstack-backend.onrender.com",
        "SPENDSTACK_API_TOKEN": "${input:spendstackApiToken}"
      }
    }
  }
}
```

## Read-only tools

- `get_wallet`
- `list_savings_locks`, `list_scheduled_payouts`
- `list_transactions` (optionally filtered by an owned statement ID)
- `list_statements`, `get_statement_details`
- `list_budgets`, `list_recurring_bills`, `list_spending_alerts`
- `get_spending_overview`, `get_category_breakdown`, `get_cash_flow`, `get_top_merchants`, `get_spending_insights`, `get_monthly_trend`, `get_monthly_comparison`

## Focused unit tests

From `backend\mcp_server`, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use `httpx.MockTransport`; no live API or credentials are needed.