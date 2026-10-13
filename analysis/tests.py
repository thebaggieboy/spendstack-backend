import io
import os
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from decimal import Decimal
import pandas as pd
from rest_framework.test import APIClient

from .models import Statement
from transactions.models import Category, Transaction

from .utils.parser import (
    parse_csv_statement_detailed,
    parse_excel_statement_detailed,
    parse_pdf_statement,
    extract_statement_summary,
)
from .utils.categorizer import categorize_transactions
from .utils.advisor import generate_financial_advice
from .views import _statement_reconciliation


def _make_test_pdf():
    stream = (
        b"BT /F1 10 Tf 20 100 Td "
        b"(01/10/2026 01/10/2026 POS/PUR/Corner Cafe 1,250.00 10,000.00 DR) Tj ET\n"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 150] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


class PdfStatementParserTests(SimpleTestCase):
    def test_csv_extracts_explicit_statement_totals_and_balances(self):
        summary = extract_statement_summary(
            b"Opening Balance,1000.00\nTotal Credits,500.00 CR\nTotal Debits,200.00 DR\nClosing Balance,1300.00\n",
            "statement.csv",
        )

        self.assertEqual(summary, {
            "opening_balance": "1000.00",
            "closing_balance": "1300.00",
            "total_credits": "500.00",
            "total_debits": "200.00",
        })

    @patch("analysis.utils.parser.pdfplumber.open")
    def test_pdf_extracts_explicit_statement_summary(self, pdf_open):
        page = MagicMock()
        page.extract_text.return_value = (
            "Opening Balance 1,000.00\nTotal Credits 500.00\n"
            "Total Debits 200.00\nClosing Balance 1,300.00"
        )
        pdf_open.return_value.__enter__.return_value.pages = [page]

        summary = extract_statement_summary(b"%PDF-summary", "statement.pdf")

        self.assertEqual(summary, {
            "opening_balance": "1000.00",
            "closing_balance": "1300.00",
            "total_credits": "500.00",
            "total_debits": "200.00",
        })

    def test_csv_marks_conflicting_statement_summary_values_as_ambiguous(self):
        summary = extract_statement_summary(
            b"Total Credits,500.00\nTotal Credits,600.00\n",
            "statement.csv",
        )

        self.assertEqual(summary, {"ambiguous_fields": ["total_credits"]})

    def test_excel_extracts_labelled_statement_summary(self):
        workbook = io.BytesIO()
        pd.DataFrame([
            ["Opening Balance", 1000],
            ["Total Credits", 500],
            ["Total Debits", 200],
            ["Closing Balance", 1300],
        ]).to_excel(workbook, header=False, index=False)

        summary = extract_statement_summary(workbook.getvalue(), "statement.xlsx")

        self.assertEqual(summary, {
            "opening_balance": "1000.00",
            "closing_balance": "1300.00",
            "total_credits": "500.00",
            "total_debits": "200.00",
        })


    def test_integration_fixture_is_a_valid_readable_pdf(self):
        import pdfplumber

        with pdfplumber.open(io.BytesIO(_make_test_pdf())) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            self.assertIn("Corner Cafe", pdf.pages[0].extract_text())

    @patch.dict(os.environ, {"GEMINI_API_KEY": "", "NEXT_GEMINI_API_KEY": ""})
    def test_searchable_pdf_is_parsed_locally_without_gemini_credentials(self):
        self.assertEqual(
            parse_pdf_statement(io.BytesIO(_make_test_pdf())),
            [{
                "date": "2026-10-01",
                "raw_description": "POS/PUR/Corner Cafe",
                "amount": Decimal("-1250.00"),
                "source_row": 1,
                "source_page": 1,
            }],
        )

    @patch("pypdfium2.PdfDocument")
    @patch("analysis.utils.parser.pdfplumber.open")
    def test_scanned_pdf_returns_actionable_local_parsing_error(self, pdf_open, pdfium_document):
        pdf = MagicMock()
        pdf.pages = [MagicMock()]
        pdf.pages[0].extract_text.return_value = None
        pdf_open.return_value.__enter__.return_value = pdf
        pdfium_document.return_value.__len__.return_value = 0

        with self.assertRaisesMessage(ValueError, "no selectable text"):
            parse_pdf_statement(io.BytesIO(b"%PDF-test"))

    @patch("pypdfium2.PdfDocument")
    @patch("analysis.utils.parser.pdfplumber.open", side_effect=ValueError("missing Root"))
    def test_pdfium_fallback_reads_text_when_pdfplumber_cannot_open_pdf(self, _pdf_open, pdf_document):
        text_page = MagicMock()
        text_page.get_text_range.return_value = (
            "01/10/2026 POS purchase Corner Cafe 1,250.00 DR"
        )
        page = MagicMock()
        page.get_textpage.return_value = text_page
        document = MagicMock()
        document.__len__.return_value = 1
        document.__getitem__.return_value = page
        pdf_document.return_value = document

        transactions = parse_pdf_statement(io.BytesIO(b"%PDF-recoverable"))

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["raw_description"], "POS purchase Corner Cafe")
        self.assertEqual(transactions[0]["amount"], Decimal("-1250.00"))
        text_page.close.assert_called_once()
        page.close.assert_called_once()
        document.close.assert_called_once()

    @patch("pypdfium2.PdfDocument", side_effect=ValueError("missing Root"))
    @patch("analysis.utils.parser.pdfplumber.open", side_effect=ValueError("missing Root"))
    def test_invalid_pdf_gets_actionable_error_instead_of_library_exception(
        self, _pdf_open, _pdfium_document
    ):
        with self.assertRaisesMessage(ValueError, "Download it again from your bank"):
            parse_pdf_statement(io.BytesIO(b"not a pdf"))

    def test_unsigned_amount_column_uses_description_to_infer_debit_or_credit(self):
        transactions, warnings = parse_csv_statement_detailed(
            b"Date,Description,Amount\n"
            b"2026-01-10,POS purchase at Corner Cafe,1250.00\n"
            b"2026-01-11,Salary credit,50000.00\n"
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [transaction["amount"] for transaction in transactions],
            [Decimal("-1250.00"), Decimal("50000.00")],
        )

    def test_csv_finds_headers_after_preamble_and_parses_european_amounts(self):
        transactions, warnings = parse_csv_statement_detailed(
            b"Bank statement export\n"
            b"Transaction Date;Narration;Debit Amount;Credit Amount\n"
            b"2026-10-01;Cafe purchase;1.234,50;\n"
            b"2026-10-02;Salary credit;;50.000,25\n"
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [(item["date"], item["amount"], item["source_row"]) for item in transactions],
            [
                ("2026-10-01", Decimal("-1234.50"), 3),
                ("2026-10-02", Decimal("50000.25"), 4),
            ],
        )

    def test_csv_ambiguous_unsigned_amount_is_reported_not_guessed(self):
        transactions, warnings = parse_csv_statement_detailed(
            b"Date,Description,Amount\n2026-01-10,Local Grocer,1250.00\n"
        )

        self.assertEqual(transactions[0]["amount"], Decimal("1250.00"))
        self.assertIn("amount sign is ambiguous", warnings[0])
        self.assertIn("Row 2", warnings[0])

    def test_excel_parser_reads_transaction_tables_on_each_sheet(self):
        workbook = io.BytesIO()
        with pd.ExcelWriter(workbook) as writer:
            pd.DataFrame([["Account summary"], ["Opening balance", 1000]]).to_excel(
                writer, sheet_name="Summary", header=False, index=False
            )
            pd.DataFrame([
                {"Date": "2026-10-01", "Description": "POS purchase", "Debit": 100},
            ]).to_excel(writer, sheet_name="Transactions", index=False)

        transactions, warnings = parse_excel_statement_detailed(workbook.getvalue())

        self.assertEqual(warnings, [])
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["amount"], Decimal("-100"))
        self.assertEqual(transactions[0]["source_sheet"], "Transactions")

    def test_excel_parser_treats_dash_amount_placeholders_as_empty_cells(self):
        workbook = io.BytesIO()
        pd.DataFrame([
            {
                "Date": "2026-10-01",
                "Description": "POS purchase",
                "Debit": "--",
                "Credit": "NGN 1,250.00 CR",
            },
            {
                "Date": "2026-10-02",
                "Description": "POS purchase",
                "Debit": "₦ 2,500.00 DR",
                "Credit": "--",
            },
        ]).to_excel(workbook, index=False)

        transactions, warnings = parse_excel_statement_detailed(workbook.getvalue())

        self.assertEqual(warnings, [])
        self.assertEqual(
            [transaction["amount"] for transaction in transactions],
            [Decimal("1250.00"), Decimal("-2500.00")],
        )

    def test_opay_xlsx_layout_parses_debit_credit_dash_placeholders(self):
        workbook = io.BytesIO()
        with pd.ExcelWriter(workbook) as writer:
            pd.DataFrame([
                ["Wallet Account Statement"],
                ["Account Name", "Example"],
                ["Account Type", "Wallet Account"],
                ["Opening Balance", "₦0.00", "Total Debit", "₦1,000.00"],
                [None],
                [
                    "Trans. Date",
                    "Value Date",
                    "Description",
                    "Debit(₦)",
                    "Credit(₦)",
                    "Balance After(₦)",
                ],
                ["11 Sep 2026 07:17:07", "11 Sep 2026", "Wallet credit", "--", "87,000.00", "87,000.00"],
                ["11 Sep 2026 07:17:02", "11 Sep 2026", "Transfer to savings", "87,000.00", "--", "0.00"],
            ]).to_excel(writer, sheet_name="Wallet Account Transactions", header=False, index=False)

        transactions, warnings = parse_excel_statement_detailed(workbook.getvalue())

        self.assertEqual(warnings, [])
        self.assertEqual(
            [transaction["amount"] for transaction in transactions],
            [Decimal("87000.00"), Decimal("-87000.00")],
        )
        self.assertTrue(all(
            transaction["source_sheet"] == "Wallet Account Transactions"
            for transaction in transactions
        ))

    def test_parser_preserves_all_rows_in_a_ten_thousand_row_csv(self):
        rows = ["Date,Description,Debit,Credit"]
        rows.extend(
            f"2026-01-{index % 28 + 1:02d},POS purchase {index},{index + 1},"
            for index in range(10000)
        )
        transactions, warnings = parse_csv_statement_detailed("\n".join(rows).encode())

        self.assertEqual(len(transactions), 10000)
        self.assertEqual(warnings, [])


class StatementReconciliationTests(SimpleTestCase):
    def test_printed_summary_mismatch_is_flagged_without_replacing_row_totals(self):
        reconciliation = _statement_reconciliation(
            {
                "total_credits": "500.00",
                "total_debits": "250.00",
                "opening_balance": "1000.00",
                "closing_balance": "1250.00",
            },
            Decimal("500.00"),
            Decimal("-200.00"),
        )

        self.assertEqual(reconciliation["status"], "mismatch")
        self.assertEqual(reconciliation["checks"]["total_debits"]["from_transactions"], 200.0)
        self.assertEqual(reconciliation["checks"]["total_debits"]["reported"], 250.0)
        self.assertEqual(reconciliation["balance_movement"]["status"], "mismatch")


class StatementPreviewApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="statement-preview@example.com",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.csv_content = b"Date,Description,Amount\n2026-01-10,Corner Store,-1250.50\n"

    def upload(self):
        return SimpleUploadedFile("january.csv", self.csv_content, content_type="text/csv")

    def test_statement_list_includes_first_and_last_transaction_dates(self):
        statement = Statement.objects.create(user=self.user, filename="period.csv")
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2025, 12, 31),
            amount=Decimal("-100.00"),
            raw_description="December purchase",
        )
        Transaction.objects.create(
            user=self.user,
            statement=statement,
            date=date(2026, 1, 2),
            amount=Decimal("-50.00"),
            raw_description="January purchase",
        )
        empty_statement = Statement.objects.create(user=self.user, filename="empty.csv")

        response = self.client.get("/api/analysis/statements/")

        self.assertEqual(response.status_code, 200)
        by_filename = {item["filename"]: item for item in response.data}
        self.assertEqual(by_filename["period.csv"]["transaction_count"], 2)
        self.assertEqual(by_filename["period.csv"]["period_start"], "2025-12-31")
        self.assertEqual(by_filename["period.csv"]["period_end"], "2026-01-02")
        self.assertIsNone(by_filename[empty_statement.filename]["period_start"])
        self.assertIsNone(by_filename[empty_statement.filename]["period_end"])

    def test_empty_pdf_upload_reports_that_the_uploaded_file_is_zero_bytes(self):
        upload = SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf")

        response = self.client.post(
            "/api/analysis/preview/",
            {"file": upload},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("empty (0 bytes)", response.data["detail"])

    def test_xlsx_statement_preview_parses_debit_credit_and_categorizes_locally(self):
        workbook = io.BytesIO()
        pd.DataFrame(
            [
                {"Date": "2026-01-10", "Description": "POS/PUR/Corner Cafe", "Debit": 1250.5, "Credit": None},
                {"Date": "2026-01-11", "Description": "Salary", "Debit": None, "Credit": 50000},
            ]
        ).to_excel(workbook, index=False)
        upload = SimpleUploadedFile(
            "january.xlsx",
            workbook.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(
            "/api/analysis/preview/",
            {"file": upload},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [transaction["amount"] for transaction in response.data["transactions"]],
            ["-1250.50", "50000.00"],
        )
        self.assertEqual(
            [transaction["category"] for transaction in response.data["transactions"]],
            ["Food & Dining", "Salary"],
        )

    def test_preview_requires_confirmation_before_creating_statement_transactions(self):
        response = self.client.post("/api/analysis/preview/", {"file": self.upload()}, format="multipart")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["transactions"]), 1)
        self.assertEqual(Statement.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)

        confirmation = self.client.post(
            f"/api/analysis/preview/{response.data['preview_id']}/confirm/",
            {},
            format="json",
        )

        self.assertEqual(confirmation.status_code, 201)
        self.assertEqual(Statement.objects.count(), 1)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(Transaction.objects.get().amount, Decimal("-1250.50"))

    def test_import_persists_summary_and_reconciliation_compares_all_posted_rows(self):
        content = (
            b"Date,Description,Debit,Credit\n"
            b"Opening Balance,1000.00,,\n"
            b"2026-01-10,Transfer to savings,200.00,\n"
            b"2026-01-11,Salary,,500.00\n"
            b"Total Credits,500.00,,\n"
            b"Total Debits,200.00,,\n"
            b"Closing Balance,1300.00,,\n"
        )
        upload = SimpleUploadedFile("balances.csv", content, content_type="text/csv")
        preview = self.client.post("/api/analysis/preview/", {"file": upload}, format="multipart")
        self.assertEqual(preview.status_code, 201, preview.data)
        self.assertEqual(preview.data["statement_summary"]["opening_balance"], "1000.00")

        confirmation = self.client.post(
            f"/api/analysis/preview/{preview.data['preview_id']}/confirm/",
            {},
            format="json",
        )
        self.assertEqual(confirmation.status_code, 201, confirmation.data)
        statement = Statement.objects.get()
        self.assertEqual(statement.summary_data["closing_balance"], "1300.00")

        detail = self.client.get(f"/api/analysis/statements/{statement.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["stats"]["total_credits"], 500.0)
        self.assertEqual(detail.data["stats"]["total_expenses"], 200.0)
        self.assertEqual(detail.data["stats"]["highest_daily_spend"], 0.0)
        self.assertEqual(detail.data["statement_reconciliation"]["status"], "matched")

    def test_legacy_direct_upload_persists_statement_summary(self):
        content = (
            b"Date,Description,Debit,Credit\n"
            b"Opening Balance,1000.00,,\n"
            b"2026-01-10,POS purchase,100.00,\n"
            b"Total Credits,0.00,,\n"
            b"Total Debits,100.00,,\n"
            b"Closing Balance,900.00,,\n"
        )
        upload = SimpleUploadedFile("legacy.csv", content, content_type="text/csv")

        response = self.client.post("/api/analysis/upload/", {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 201, response.data)
        statement = Statement.objects.get()
        self.assertEqual(statement.summary_data["opening_balance"], "1000.00")
        self.assertEqual(statement.summary_data["closing_balance"], "900.00")

    def test_preview_detects_already_imported_file(self):
        preview = self.client.post(
            "/api/analysis/preview/",
            {"file": self.upload()},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 201, preview.data)
        self.client.post(
            f"/api/analysis/preview/{preview.data['preview_id']}/confirm/",
            {},
            format="json",
        )

        duplicate = self.client.post(
            "/api/analysis/preview/",
            {"file": self.upload()},
            format="multipart",
        )

        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("already been uploaded", duplicate.data["detail"])

    def test_preview_blocks_import_if_any_transaction_row_is_ambiguous(self):
        content = (
            b"Date,Description,Amount\n"
            b"2026-01-10,POS purchase at Cafe,-1250.50\n"
            b"2026-01-11,Local Grocer,450.00\n"
        )
        upload = SimpleUploadedFile("ambiguous.csv", content, content_type="text/csv")

        preview = self.client.post("/api/analysis/preview/", {"file": upload}, format="multipart")

        self.assertEqual(preview.status_code, 201, preview.data)
        self.assertEqual(len(preview.data["transactions"]), 2)
        self.assertIn("amount sign is ambiguous", preview.data["warnings"][0])
        confirmation = self.client.post(
            f"/api/analysis/preview/{preview.data['preview_id']}/confirm/",
            {},
            format="json",
        )

        self.assertEqual(confirmation.status_code, 400)
        self.assertEqual(Statement.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_import_preserves_descriptions_longer_than_old_500_character_limit(self):
        description = ("POS purchase at " + ("Long Merchant " * 50)).rstrip()
        content = f"Date,Description,Amount\n2026-01-10,{description},-1250.50\n".encode()
        upload = SimpleUploadedFile("long.csv", content, content_type="text/csv")

        preview = self.client.post("/api/analysis/preview/", {"file": upload}, format="multipart")
        self.assertEqual(preview.status_code, 201, preview.data)
        confirmation = self.client.post(
            f"/api/analysis/preview/{preview.data['preview_id']}/confirm/",
            {},
            format="json",
        )

        self.assertEqual(confirmation.status_code, 201, confirmation.data)
        transaction = Transaction.objects.get()
        self.assertEqual(transaction.raw_description, description)

    def test_confirm_import_bulk_persists_large_statement_without_losing_rows(self):
        row_count = 1001
        rows = ["Date,Description,Debit,Credit"]
        rows.extend(
            f"2026-01-{index % 28 + 1:02d},POS purchase {index},125.50,"
            for index in range(row_count)
        )
        upload = SimpleUploadedFile(
            "large-statement.csv",
            "\n".join(rows).encode(),
            content_type="text/csv",
        )

        preview = self.client.post(
            "/api/analysis/preview/",
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 201, preview.data)
        self.assertEqual(len(preview.data["transactions"]), row_count)

        confirmation = self.client.post(
            f"/api/analysis/preview/{preview.data['preview_id']}/confirm/",
            {},
            format="json",
        )

        self.assertEqual(confirmation.status_code, 201, confirmation.data)
        self.assertEqual(confirmation.data["transaction_count"], row_count)
        self.assertEqual(Transaction.objects.count(), row_count)


class StatementCategorizerTests(SimpleTestCase):
    def test_categorizes_using_description_and_signed_amount(self):
        transactions = [
            {"raw_description": "NIP/TRF/123456/JOHN DOE", "amount": "-8000.00"},
            {"raw_description": "POS/PUR/ABC SUPERMARKET", "amount": "-2500.00"},
            {"raw_description": "Monthly salary", "amount": "250000.00"},
            {"raw_description": "SMS Alert Fee", "amount": "-10.00"},
        ]

        result = categorize_transactions(transactions)

        self.assertEqual(
            [transaction["category_name"] for transaction in result],
            ["Transfers", "Food & Dining", "Salary", "Bank Charges"],
        )
        self.assertEqual(
            [transaction["merchant_name"] for transaction in result],
            ["JOHN DOE", "ABC SUPERMARKET", "Monthly salary", "SMS Alert Fee"],
        )


class LocalFinancialInsightTests(TestCase):
    def test_dashboard_insights_use_recent_spending_without_gemini_credentials(self):
        user = get_user_model().objects.create_user(
            email="local-insights@example.com",
            password="test-password",
        )
        category = Category.objects.create(name="Local insight dining")
        Transaction.objects.create(
            user=user,
            date=timezone.localdate(),
            amount=Decimal("-1250.00"),
            merchant_name="Local Cafe",
            raw_description="Cafe purchase",
            category=category,
        )
        Transaction.objects.create(
            user=user,
            date=timezone.localdate(),
            amount=Decimal("-9000.00"),
            merchant_name="Own account",
            raw_description="NIP transfer",
            is_transfer=True,
        )

        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "NEXT_GEMINI_API_KEY": ""}):
            insights = generate_financial_advice(user.id)

        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0]["title"], "Spending in Local insight dining")
        self.assertIn("₦1,250.00", insights[0]["message"])
