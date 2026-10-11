import pandas as pd
import pdfplumber
import io
import os
import json
import base64
from google import genai

GEMINI_PDF_MODEL = "gemini-3.8-flash"

def parse_csv_statement(file_content):
    transactions, _ = parse_csv_statement_detailed(file_content)
    return transactions


def parse_csv_statement_detailed(file_content):
    """
    Basic heuristic-based CSV parser.
    Assumes common bank structures (Date, Description, Amount).
    Returns accepted rows alongside row-specific warnings.
    """
    try:
        df = pd.read_csv(io.StringIO(file_content.decode("utf-8-sig")))
        return _parse_tabular_statement(df)
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {str(e)}")


def parse_excel_statement_detailed(file_content):
    """Parse the first worksheet in an .xlsx or .xls bank statement."""
    try:
        df = pd.read_excel(io.BytesIO(file_content), sheet_name=0)
        return _parse_tabular_statement(df)
    except Exception as e:
        raise ValueError(f"Failed to parse Excel statement: {str(e)}")


def _parse_tabular_statement(df):
    df.columns = [str(column).strip().lower() for column in df.columns]
    col_names = list(df.columns)

    date_col = next((c for c in col_names if "date" in c), None)
    desc_col = next(
        (c for c in col_names if any(term in c for term in ("description", "name", "details", "remarks"))),
        None,
    )
    amount_col = next((c for c in col_names if "amount" in c), None)
    debit_col = next((c for c in col_names if "debit" in c or "withdrawal" in c), None)
    credit_col = next((c for c in col_names if "credit" in c or "deposit" in c), None)

    if not date_col or not desc_col:
        raise ValueError("Could not auto-detect Date and Description columns.")
    if not amount_col and not (debit_col and credit_col):
        raise ValueError("Could not auto-detect Amount or Debit/Credit columns.")

    transactions = []
    warnings = []
    for index, row in df.iterrows():
        if pd.isna(row[date_col]) or pd.isna(row[desc_col]):
            warnings.append(f"Row {index + 2}: missing date or description; skipped.")
            continue

        amount_val = 0.0
        if amount_col and not pd.isna(row[amount_col]):
            raw_amount = str(row[amount_col]).replace("₦", "").replace("$", "").replace(",", "")
            try:
                amount_val = float(raw_amount)
            except ValueError:
                warnings.append(f"Row {index + 2}: invalid amount; skipped.")
                continue
        elif debit_col and not pd.isna(row[debit_col]):
            try:
                amount_val = -float(str(row[debit_col]).replace("₦", "").replace(",", ""))
            except ValueError:
                warnings.append(f"Row {index + 2}: invalid debit amount; skipped.")
                continue
        elif credit_col and not pd.isna(row[credit_col]):
            try:
                amount_val = float(str(row[credit_col]).replace("₦", "").replace(",", ""))
            except ValueError:
                warnings.append(f"Row {index + 2}: invalid credit amount; skipped.")
                continue

        if amount_val == 0.0:
            warnings.append(f"Row {index + 2}: zero or missing amount; skipped.")
            continue

        try:
            dt = pd.to_datetime(str(row[date_col]).strip(), dayfirst=True)
            date_val = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            warnings.append(f"Row {index + 2}: invalid date; skipped.")
            continue

        transactions.append({
            "date": date_val,
            "raw_description": str(row[desc_col]),
            "amount": amount_val,
        })

    return transactions, warnings


def parse_pdf_statement(file_obj):
    """
    Extract transactions locally when possible, then use Gemini's Interactions API.
    PDFs without extractable text are sent inline as a document, avoiding temporary
    Gemini Files API resources and keeping model calls on the supported endpoint.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY must be set for PDF extraction.")

    client = genai.Client(api_key=api_key)

    # --- Strategy 1: Try pdfplumber text extraction ---
    text_content = ""
    use_inline_pdf = False

    try:
        # Read file bytes once so we can reuse them if pdfplumber fails
        file_bytes = file_obj.read()
        file_obj.seek(0)  # reset in case caller needs to re-read

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text_content += extracted + "\n"

        if not text_content.strip():
            use_inline_pdf = True

    except Exception:
        use_inline_pdf = True

    PROMPT_INSTRUCTIONS = """
You are a precise data extraction AI processing a Nigerian bank statement.
Extract ALL transaction records from the document.
Convert them into a JSON array of objects. Each object MUST have:
- "date": string, formatted strictly as YYYY-MM-DD. Nigerian statements use DD/MM/YYYY or DD/MM/YY — convert accordingly.
- "raw_description": string, the full raw description/narration of the transaction.
- "amount": float, the transaction amount. Debits/withdrawals/expenses MUST be negative. Credits/deposits/income MUST be positive.

If there are no transactions, return an empty array [].
Return ONLY a valid JSON array with no markdown formatting or extra explanation.
"""

    try:
        if use_inline_pdf:
            interaction_input = [
                {
                    "type": "document",
                    "data": base64.b64encode(file_bytes).decode("ascii"),
                    "mime_type": "application/pdf",
                },
                {"type": "text", "text": PROMPT_INSTRUCTIONS},
            ]
        else:
            prompt = f"{PROMPT_INSTRUCTIONS}\n\nRaw text:\n{text_content}"
            interaction_input = [{"type": "text", "text": prompt}]

        response = client.interactions.create(
            model=GEMINI_PDF_MODEL,
            input=interaction_input,
            store=False,
        )
        res_text = response.output_text
        if not res_text:
            raise ValueError("Gemini returned an empty response for the PDF.")
        res_text = res_text.strip()
        if res_text.startswith("```json"):
            res_text = res_text[7:]
        if res_text.startswith("```"):
            res_text = res_text[3:]
        if res_text.endswith("```"):
            res_text = res_text[:-3]

        transactions = json.loads(res_text.strip())
        return transactions
    except Exception as e:
        raise ValueError(f"Failed to extract PDF: {str(e)}")
