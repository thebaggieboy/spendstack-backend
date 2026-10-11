import io
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

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
