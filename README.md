# spendstack-backend

## Internal product analytics

Run `python manage.py migrate` to create the page-visit table. The frontend records same-site page paths and timestamps with a random first-party browser ID; it does not send IP addresses or URL query strings. Visits are associated with the signed-in account when an access token is available. Tracking begins after this version is deployed, so earlier page visits are not backfilled.

The analytics dashboard is at `/admin/analytics` and is available only to authenticated staff/superusers. Its data endpoints enforce the same restriction:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/internal-analytics/track/` | Record a page visit (rate-limited; no IP address is stored) |
| `GET` | `/api/internal-analytics/overview/` | Read signup, active-user, page-view, statement, transaction, and Pro subscription metrics |
| `GET` | `/api/internal-analytics/users/?page=1&search=...` | Search and page through account-level activity and usage |

“Active users” counts distinct signed-in accounts with a tracked page visit in the stated period. “Unique browsers” counts distinct first-party browser IDs, including anonymous visitors. Admin metrics are operational product telemetry, not a substitute for consent, retention, or privacy-policy requirements.

## Spending tools

Statement imports apply each user's active transaction rules to the parsed description and merchant. Rules can rename merchants, assign a category, and explicitly mark or exempt a matching transaction as a transfer. Description detection recognizes common transfer narration (including NIP/TRF formats); transfer transactions remain visible in history but are excluded from income, expense, cash-flow, insights, and spending-alert totals. Existing transactions are backfilled during migration `transactions.0004`.

Authenticated endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/dashboard/chat/` | Ask questions about all of the signed-in user's transactions or one owned statement; returns a Gemini-generated answer |
| `GET` | `/api/dashboard/monthly-comparison/` | Compare current month-to-date income/spending with the same calendar days of the previous month, including category changes; transfers excluded |
| `GET` | `/api/dashboard/recurring-bills/` | Detect weekly, biweekly, or monthly repeat outflows from at least three similar transactions |
| `GET, POST` | `/api/transactions/alerts/` | List current weekly/monthly spending thresholds or create an alert |
| `PATCH, DELETE` | `/api/transactions/alerts/{id}/` | Change or remove one of the signed-in user's alerts |
| `GET, POST` | `/api/transactions/rules/` | List or create transaction description/merchant rules |
| `PATCH, DELETE` | `/api/transactions/rules/{id}/` | Change or remove one of the signed-in user's rules |

Spending-alert responses include current-period spend and whether the threshold has been reached. These are in-app alerts; no email, SMS, or push delivery is configured. Rules are reapplied to the owner's existing transactions when created, updated, or removed.

Statement chat requests accept a `question`, an optional owned `statement_id`, and optional recent `history` messages (`role` and `content`). The endpoint summarizes only the authenticated user's statement data and sends a bounded transaction context to Gemini. Set `NEXT_GEMINI_API_KEY` in the backend environment (or use `GEMINI_API_KEY`); keep this key server-side and never expose it through a `NEXT_PUBLIC_` variable.

Statement uploads support a review-before-import flow:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/analysis/preview/` | Parse and categorize CSV, Excel (`.xlsx`/`.xls`), or PDF files (maximum 10 MB), apply the user's rules, and return a temporary transaction preview plus skipped-row warnings |
| `POST` | `/api/analysis/preview/{id}/confirm/` | Import the preview after user confirmation; previews expire after one hour |
| `DELETE` | `/api/analysis/preview/{id}/` | Discard a preview |
| `POST` | `/api/analysis/upload/` | Legacy direct-import endpoint; exact duplicate files are rejected |

Imported statement files are fingerprinted with SHA-256 per user, so uploading the same bytes again returns HTTP 409. This is exact-file duplicate detection; files with different bytes are not deduplicated by transaction content. Chat answers may include `[TX-id]` references; the API returns validated source metadata for those IDs so the frontend can link back to the owning statement transaction.

PDF extraction uses `google-genai` 2.x for the current Gemini Interactions API schema. The Gemini PDF integration test is opt-in because it sends a test PDF to the live Gemini API and may incur API usage. From `backend/`, set `RUN_GEMINI_INTEGRATION_TESTS=1` and `GEMINI_API_KEY` (or `NEXT_GEMINI_API_KEY`), then run `python manage.py test analysis.tests.LiveGeminiPdfIntegrationTests`. Without the opt-in flag or key, Django reports the test as skipped.

## Wallet, money locks, and scheduled payouts

Set `PAYSTACK_SECRET_KEY` in the backend environment. The value must be a server-side Paystack secret key and must never be exposed to the frontend. Set `SPENDSTACK_FRONTEND_URL` to the deployed frontend origin so Paystack can return users to the wallet page. Apply migrations with `python manage.py migrate`.

Authenticated endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/savings/wallet/` | Read available/locked balances and recent ledger entries |
| `POST` | `/api/savings/fund/initialize/` | Initialize a Paystack Checkout top-up |
| `GET` | `/api/savings/fund/verify/?reference=...` | Verify a checkout payment and credit it once |
| `GET, POST` | `/api/savings/locks/` | List locks or reserve available wallet funds until maturity |
| `GET` | `/api/savings/bank-directory/` | List banks supported by Paystack |
| `GET, POST` | `/api/savings/bank-accounts/` | List the user's verified recipients or verify/register one |
| `GET, POST` | `/api/savings/budgets/` | List or create a spending budget |
| `DELETE` | `/api/savings/budgets/{id}/` | Delete one of the signed-in user's budgets |
| `GET` | `/api/savings/schedules/` | List the signed-in user's scheduled payouts |
| `POST` | `/api/savings/schedules/{id}/payout/` | Start a due payout after explicit user confirmation |
| `POST` | `/api/savings/webhooks/paystack/` | Receive signed Paystack charge and transfer events |

Configure Paystack to send `charge.success`, `transfer.success`, `transfer.failed`, and `transfer.reversed` events to `https://<backend-host>/api/savings/webhooks/paystack/`. The webhook validates Paystack's HMAC-SHA512 signature; successful charges are independently verified with Paystack before wallet credit. A user's full locked amount remains reserved until a verified transfer-success event completes its payout. Failed transfers do not release the lock.

To enable automatic payouts, schedule this command to run periodically (for example, every 5 minutes) in the deployment platform's cron service:

```sh
python manage.py process_due_payouts
```

The application ledger tracks user-attributed balances; it does not itself segregate or custody funds. Reconcile and segregate settlement funds through the configured regulated/custodial arrangement before enabling real-money production use. Test checkout and transfers with Paystack test credentials first. No yield or interest is calculated.

## Paystack Pro subscriptions

Create an active **NGN monthly** plan in Paystack, then set these backend environment variables:

- `PAYSTACK_SECRET_KEY`: server-only Paystack secret key.
- `PAYSTACK_PRO_PLAN_CODE`: plan code for the configured monthly Pro plan.
- `BILLING_TOKEN_ENCRYPTION_KEY`: a Fernet key used to encrypt Paystack subscription-management tokens at rest. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and store it as a secret. Do not rotate it without first re-encrypting stored subscription tokens.
- `SPENDSTACK_FRONTEND_URL`: frontend origin used for the checkout return URL.

Run `python manage.py migrate` after deploying. The existing signed webhook endpoint at `/api/savings/webhooks/paystack/` handles wallet events and Pro subscription events; configure that **single** URL in Paystack and subscribe to `charge.success`, `transfer.success`, `transfer.failed`, `transfer.reversed`, `subscription.create`, `subscription.disable`, `subscription.not_renew`, `invoice.payment_successful`, and `invoice.payment_failed`. Subscription access is granted only from a signed matching subscription event. The frontend payment-return verification independently checks the transaction and checkout ownership but does not activate a plan on its own.

Authenticated subscription endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/billing/plans/` | Read the configured Paystack Pro plan and monthly price |
| `GET` | `/api/billing/subscription/` | Read the signed-in user's subscription and current Pro entitlement |
| `POST` | `/api/billing/checkout/` | Start Paystack checkout for the configured plan |
| `GET` | `/api/billing/checkout/verify/?reference=...` | Verify payment ownership, plan amount, and transaction status |
| `POST` | `/api/billing/subscription/cancel/` | Cancel renewal through Paystack; retain access through the paid period |

Free accounts keep core transaction/statement tracking, can chat with their statements, and can read existing rules/alerts. Pro-only features include personalized AI advice, advanced analytics/report export, recurring-bill detection, and creating/updating automated spending alerts and transaction rules. Django enforces the gates server-side; the frontend upsell is not the access-control boundary. Saved tax scenarios and statement-file exports are not currently implemented.

## Local read-only MCP server

The standalone local stdio MCP adapter and setup/client configuration examples are in [`mcp_server/README.md`](./mcp_server/README.md). It calls the existing authenticated API with the user's Spendstack access token, exposes read-only tools, and does not access the database directly. From `backend/`, install the separate dependencies and run it with:

```powershell
python -m pip install -r requirements-mcp.txt
$env:SPENDSTACK_API_URL = "https://spendstack-backend.onrender.com"
$env:SPENDSTACK_API_TOKEN = "<your Spendstack API access token>"
python -m spendstack_mcp.server
```

For this command, use the `mcp_server/` directory as the working directory; `SPENDSTACK_API_URL` is the API origin without `/api`. Configure the MCP client to launch this process over stdio and pass the token through its environment configuration, not into a committed config file. Do not add this adapter dependency set to deployed Django requirements unless hosting the MCP server there.
