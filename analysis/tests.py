import io
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from decimal import Decimal
from rest_framework.test import APIClient

from .models import Statement
from transactions.models import Transaction

from .utils.parser import GEMINI_PDF_MODEL, parse_pdf_statement


class PdfStatementParserTests(SimpleTestCase):
    @patch("analysis.utils.parser.genai.Client")
    @patch("analysis.utils.parser.pdfplumber.open")
    def test_text_extraction_uses_current_gemini_model(self, pdf_open, client_factory):
        page = MagicMock()
        page.extract_text.return_value = "Statement transactions"
        pdf = MagicMock()
        pdf.pages = [page]
        pdf_open.return_value.__enter__.return_value = pdf
        client_factory.return_value.models.generate_content.return_value.text = "[]"

        result = parse_pdf_statement(io.BytesIO(b"%PDF-test"))

        self.assertEqual(result, [])
        client_factory.return_value.models.generate_content.assert_called_once()
        self.assertEqual(
            client_factory.return_value.models.generate_content.call_args.kwargs["model"],
            GEMINI_PDF_MODEL,
        )

    @patch("analysis.utils.parser.genai.Client")
    @patch("analysis.utils.parser.pdfplumber.open")
    def test_uploaded_pdf_extraction_uses_current_gemini_model(self, pdf_open, client_factory):
        pdf = MagicMock()
        pdf.pages = [MagicMock()]
        pdf.pages[0].extract_text.return_value = None
        pdf_open.return_value.__enter__.return_value = pdf
        client = client_factory.return_value
        client.files.upload.return_value = "uploaded-pdf"
        client.models.generate_content.return_value.text = "[]"

        result = parse_pdf_statement(io.BytesIO(b"%PDF-test"))

        self.assertEqual(result, [])
        client.models.generate_content.assert_called_once()
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            GEMINI_PDF_MODEL,
        )


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
