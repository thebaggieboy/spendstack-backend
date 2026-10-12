import io
import os
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
)
from .utils.categorizer import categorize_transactions
from .utils.advisor import generate_financial_advice


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

    @patch("analysis.utils.parser.pdfplumber.open")
    def test_scanned_pdf_returns_actionable_local_parsing_error(self, pdf_open):
        pdf = MagicMock()
        pdf.pages = [MagicMock()]
        pdf.pages[0].extract_text.return_value = None
        pdf_open.return_value.__enter__.return_value = pdf

        with self.assertRaisesMessage(ValueError, "no selectable text"):
            parse_pdf_statement(io.BytesIO(b"%PDF-test"))

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

    def test_parser_preserves_all_rows_in_a_ten_thousand_row_csv(self):
        rows = ["Date,Description,Debit,Credit"]
        rows.extend(
            f"2026-01-{index % 28 + 1:02d},POS purchase {index},{index + 1},"
            for index in range(10000)
        )
        transactions, warnings = parse_csv_statement_detailed("\n".join(rows).encode())

        self.assertEqual(len(transactions), 10000)
        self.assertEqual(warnings, [])


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
