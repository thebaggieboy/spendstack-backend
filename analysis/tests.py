import io
import base64
import os
import unittest
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from decimal import Decimal
import pandas as pd
from rest_framework.test import APIClient

from .models import Statement
from transactions.models import Transaction

from .utils.parser import GEMINI_PDF_MODEL, parse_pdf_statement


def _make_test_pdf():
    stream = b"BT /F1 14 Tf 20 100 Td (No transactions on this statement.) Tj ET\n"
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
            self.assertIn("No transactions on this statement.", pdf.pages[0].extract_text())

    @patch("analysis.utils.parser.genai.Client")
    @patch("analysis.utils.parser.pdfplumber.open")
    def test_text_extraction_uses_current_gemini_model(self, pdf_open, client_factory):
        page = MagicMock()
        page.extract_text.return_value = "Statement transactions"
        pdf = MagicMock()
        pdf.pages = [page]
        pdf_open.return_value.__enter__.return_value = pdf
        client_factory.return_value.interactions.create.return_value.output_text = "[]"

        result = parse_pdf_statement(io.BytesIO(b"%PDF-test"))

        self.assertEqual(result, [])
        client_factory.return_value.interactions.create.assert_called_once()
        self.assertEqual(
            client_factory.return_value.interactions.create.call_args.kwargs["model"],
            GEMINI_PDF_MODEL,
        )
        self.assertEqual(
            client_factory.return_value.interactions.create.call_args.kwargs["input"][0]["type"],
            "text",
        )

    @patch("analysis.utils.parser.genai.Client")
    @patch("analysis.utils.parser.pdfplumber.open")
    def test_scanned_pdf_is_sent_inline_through_interactions_api(self, pdf_open, client_factory):
        pdf = MagicMock()
        pdf.pages = [MagicMock()]
        pdf.pages[0].extract_text.return_value = None
        pdf_open.return_value.__enter__.return_value = pdf
        client = client_factory.return_value
        client.interactions.create.return_value.output_text = "[]"

        pdf_bytes = b"%PDF-test"
        result = parse_pdf_statement(io.BytesIO(pdf_bytes))

        self.assertEqual(result, [])
        client.interactions.create.assert_called_once()
        self.assertFalse(client.files.upload.called)
        call_kwargs = client.interactions.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["model"],
            GEMINI_PDF_MODEL,
        )
        self.assertFalse(call_kwargs["store"])
        document_input = call_kwargs["input"][0]
        self.assertEqual(document_input["type"], "document")
        self.assertEqual(document_input["mime_type"], "application/pdf")
        self.assertEqual(
            base64.b64decode(document_input["data"]),
            pdf_bytes,
        )


@unittest.skipUnless(
    os.environ.get("RUN_GEMINI_INTEGRATION_TESTS") == "1"
    and (os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")),
    "Set RUN_GEMINI_INTEGRATION_TESTS=1 and GEMINI_API_KEY to run the live Gemini test.",
)
class LiveGeminiPdfIntegrationTests(SimpleTestCase):
    @patch(
        "analysis.utils.parser.pdfplumber.open",
        side_effect=RuntimeError("Force the native PDF document request."),
    )
    def test_live_interactions_api_extracts_valid_transaction_array(self, _pdf_open):
        transactions = parse_pdf_statement(io.BytesIO(_make_test_pdf()))

        self.assertIsInstance(transactions, list)
        for transaction in transactions:
            self.assertIsInstance(transaction, dict)
            self.assertRegex(transaction.get("date", ""), r"^\d{4}-\d{2}-\d{2}$")
            self.assertIsInstance(transaction.get("raw_description"), str)
            self.assertIsInstance(transaction.get("amount"), (int, float))


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

    @patch("analysis.views.categorize_transactions")
    def test_xlsx_statement_preview_parses_debit_and_credit_columns(self, categorize):
        workbook = io.BytesIO()
        pd.DataFrame(
            [
                {"Date": "2026-01-10", "Description": "Card purchase", "Debit": 1250.5, "Credit": None},
                {"Date": "2026-01-11", "Description": "Salary", "Debit": None, "Credit": 50000},
            ]
        ).to_excel(workbook, index=False)
        upload = SimpleUploadedFile(
            "january.xlsx",
            workbook.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        categorize.side_effect = lambda transactions: [
            {**tx, "merchant_name": tx["raw_description"], "category_name": "Other"}
            for tx in transactions
        ]

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

    @patch("analysis.views.categorize_transactions")
    def test_preview_requires_confirmation_before_creating_statement_transactions(self, categorize):
        categorize.side_effect = lambda transactions: [
            {**tx, "merchant_name": "Corner Store", "category_name": "Food & Dining"}
            for tx in transactions
        ]

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

    @patch("analysis.views.categorize_transactions")
    def test_preview_detects_already_imported_file(self, categorize):
        categorize.side_effect = lambda transactions: [
            {**tx, "merchant_name": "Corner Store", "category_name": "Food & Dining"}
            for tx in transactions
        ]
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
