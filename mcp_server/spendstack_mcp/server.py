"""Local read-only stdio MCP server for Spendstack."""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from .api import SpendstackAPI, SpendstackAPIError
from .config import ConfigurationError, load_settings


mcp = FastMCP("Spendstack")
_api = None


def _client():
    if _api is None:
        raise SpendstackAPIError("Spendstack MCP server is not configured.")
    return _api


@mcp.tool()
def get_wallet() -> dict:
    """Read your current wallet balances and recent wallet ledger entries."""
    return _client().get("/api/savings/wallet/")


@mcp.tool()
def list_savings_locks() -> list:
    """List your active and completed wallet savings locks."""
    return _client().get("/api/savings/locks/")


@mcp.tool()
def list_scheduled_payouts() -> list:
    """List your scheduled savings payouts and their current status."""
    return _client().get("/api/savings/schedules/")


@mcp.tool()
def list_transactions(statement_id: int | None = None) -> list:
    """List your transactions; optionally limit results to one owned statement."""
    params = {"statement_id": statement_id} if statement_id is not None else None
    return _client().get("/api/transactions/", params=params)


@mcp.tool()
def list_statements() -> list:
    """List your uploaded statements and their metadata."""
    return _client().get("/api/analysis/statements/")


@mcp.tool()
def get_statement_details(statement_id: int) -> dict:
    """Read your statement summary, transactions, category breakdown, and daily totals."""
    if statement_id <= 0:
        raise ValueError("statement_id must be a positive integer.")
    return _client().get(f"/api/analysis/statements/{statement_id}/")


@mcp.tool()
def list_budgets() -> list:
    """List your active budgets with current-period spend and remaining amounts."""
    return _client().get("/api/savings/budgets/")


@mcp.tool()
def list_recurring_bills() -> list:
    """List recurring outflows detected from your transactions."""
    return _client().get("/api/dashboard/recurring-bills/")


@mcp.tool()
def list_spending_alerts() -> list:
    """List your spending thresholds with current spend and exceeded status."""
    return _client().get("/api/transactions/alerts/")


@mcp.tool()
def get_spending_overview() -> dict:
    """Read your all-time income, spending, savings rate, and highest-spend summary."""
    return _client().get("/api/dashboard/overview/")


@mcp.tool()
def get_category_breakdown() -> list:
    """Read your spending totals grouped by category."""
    return _client().get("/api/dashboard/category-breakdown/")


@mcp.tool()
def get_cash_flow() -> list:
    """Read daily income and expense totals derived from your transactions."""
    return _client().get("/api/dashboard/cash-flow/")


@mcp.tool()
def get_top_merchants() -> list:
    """Read your leading merchants by spending."""
    return _client().get("/api/dashboard/top-merchants/")


@mcp.tool()
def get_spending_insights() -> dict:
    """Read generated spending insights for your transaction history."""
    return _client().get("/api/dashboard/insights/")


@mcp.tool()
def get_monthly_trend() -> list:
    """Read your monthly income and spending trend."""
    return _client().get("/api/dashboard/monthly-trend/")


@mcp.tool()
def get_monthly_comparison() -> dict:
    """Compare your current-month spending with the previous month."""
    return _client().get("/api/dashboard/monthly-comparison/")


def main():
    global _api
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"Spendstack MCP configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    _api = SpendstackAPI(settings.api_url, settings.api_token, settings.timeout_seconds)
    try:
        mcp.run(transport="stdio")
    finally:
        _api.close()


if __name__ == "__main__":
    main()