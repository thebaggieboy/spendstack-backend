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
