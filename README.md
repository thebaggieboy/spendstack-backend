# spendstack-backend

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
| `POST` | `/api/analysis/preview/` | Parse and categorize a CSV/PDF (maximum 10 MB), apply the user's rules, and return a temporary transaction preview plus skipped-row warnings |
| `POST` | `/api/analysis/preview/{id}/confirm/` | Import the preview after user confirmation; previews expire after one hour |
| `DELETE` | `/api/analysis/preview/{id}/` | Discard a preview |
| `POST` | `/api/analysis/upload/` | Legacy direct-import endpoint; exact duplicate files are rejected |

Imported statement files are fingerprinted with SHA-256 per user, so uploading the same bytes again returns HTTP 409. This is exact-file duplicate detection; files with different bytes are not deduplicated by transaction content. Chat answers may include `[TX-id]` references; the API returns validated source metadata for those IDs so the frontend can link back to the owning statement transaction.

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
