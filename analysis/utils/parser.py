import pandas as pd
import pdfplumber
import io
import re
import os
import json
from google import genai

def parse_csv_statement(file_content):
    """
    Basic heuristic-based CSV parser.
    Assumes common bank structures (Date, Description, Amount).
    """
    try:
        df = pd.read_csv(io.StringIO(file_content.decode('utf-8')))
        
        # Super rudimentary mapping for MVP (would need robust column detection in prod)
        # Attempt to find standard columns
        col_names = [str(c).lower() for c in df.columns]
        
        date_col = next((c for c in col_names if 'date' in c), None)
        desc_col = next((c for c in col_names if 'description' in c or 'name' in c or 'details' in c or 'remarks' in c), None)
        
        # Nigerian banks often split into Debit/Credit or Withdrawal/Deposit
        amount_col = next((c for c in col_names if 'amount' in c), None)
        debit_col = next((c for c in col_names if 'debit' in c or 'withdrawal' in c), None)
        credit_col = next((c for c in col_names if 'credit' in c or 'deposit' in c), None)

        if not date_col or not desc_col:
            raise ValueError("Could not auto-detect Date and Description columns.")
            
        if not amount_col and not (debit_col and credit_col):
            raise ValueError("Could not auto-detect Amount or Debit/Credit columns.")

        transactions = []
        for index, row in df.iterrows():
            if pd.isna(row[date_col]) or pd.isna(row[desc_col]):
                continue
                
            # Determine actual amount
            amount_val = 0.0
            if amount_col and not pd.isna(row[amount_col]):
                raw_amt = str(row[amount_col]).replace('₦', '').replace('$', '').replace(',', '')
                try:
                    amount_val = float(raw_amt)
                except ValueError:
                    continue
            else:
                # Handle split Debit / Credit
                if debit_col and not pd.isna(row[debit_col]):
                    raw_debit = str(row[debit_col]).replace('₦', '').replace(',', '')
                    try:
                        amount_val = -float(raw_debit)
                    except ValueError:
                        pass
                elif credit_col and not pd.isna(row[credit_col]):
                    raw_credit = str(row[credit_col]).replace('₦', '').replace(',', '')
                    try:
                        amount_val = float(raw_credit)
                    except ValueError:
                        pass
                        
            if amount_val == 0.0:
                continue

            # Parse date safely (assuming Nigerian day-first format usually)
            raw_date = str(row[date_col]).strip()
            try:
                # Use pandas to_datetime which is robust, handling dayfirst
                dt = pd.to_datetime(raw_date, dayfirst=True)
                date_val = dt.strftime('%Y-%m-%d')
            except Exception:
                continue

            transactions.append({
                "date": date_val,
                "raw_description": str(row[desc_col]),
                "amount": amount_val
            })
            
        return transactions
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {str(e)}")

def parse_pdf_statement(file_obj):
    """
    Extracts transactions from a PDF bank statement.
    Strategy:
    1. Try pdfplumber to get text (works for non-encrypted PDFs).
    2. If the PDF is encrypted/password-protected, fall back to uploading the raw bytes
       directly to the Gemini Files API, which can natively read owner-locked PDFs like
       most Nigerian bank statements (GTBank, UBA, Access Bank, Zenith, etc.).
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY must be set for PDF extraction.")

    client = genai.Client(api_key=api_key)

    # --- Strategy 1: Try pdfplumber text extraction ---
    text_content = ""
    use_file_upload = False

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
            # pdfplumber opened OK but extracted nothing — try Gemini with file upload
            use_file_upload = True

    except Exception:
        # Most likely PDFPasswordIncorrect or another encryption error
        use_file_upload = True

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
        if use_file_upload:
            # --- Strategy 2: Upload raw PDF bytes to Gemini Files API ---
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            try:
                uploaded_file = client.files.upload(
                    file=tmp_path,
                    config={"display_name": "bank_statement.pdf"}
                )

                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[uploaded_file, PROMPT_INSTRUCTIONS],
                )
            finally:
                os.unlink(tmp_path)  # clean up temp file

        else:
            # --- Strategy 1 continued: Send extracted text to Gemini ---
            prompt = f"{PROMPT_INSTRUCTIONS}\n\nRaw text:\n{text_content}"
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt,
            )

        res_text = response.text.strip()
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

