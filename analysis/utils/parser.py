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
    Extracts text using PDFPlumber and then asks Gemini to convert it into structured transactions.
    """
    try:
        text_content = ""
        with pdfplumber.open(file_obj) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text_content += extracted + "\n"
        
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY or NEXT_GEMINI_API_KEY must be set for PDF extraction.")
            
        client = genai.Client(api_key=api_key)
        
        prompt = f"""
        You are a precise data extraction AI. You are given the raw text extracted from a Nigerian bank statement PDF.
        Extract all the transaction records from this text.
        Look for lines that look like transaction records (Date, Description, Amount, etc.).
        Convert them into a JSON array of objects.
        Each object MUST have the following keys:
        - "date": string, the date of the transaction formatted strictly as YYYY-MM-DD. Pay attention to the fact that these are Nigerian bank statements, so the dates in the text are almost always DD/MM/YYYY or DD/MM/YY. You must convert them to YYYY-MM-DD!
        - "raw_description": string, the full raw description/narration
        - "amount": float, the amount. Expenses/debits MUST be negative. Income/credits MUST be positive.

        If the text contains no transactions, return an empty array [].
        Return ONLY valid JSON array with no markdown formatting.
        
        Raw text to process:
        {text_content}
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        res_text = response.text.strip()
        if res_text.startswith("```json"):
            res_text = res_text[7:]
        if res_text.endswith("```"):
            res_text = res_text[:-3]
            
        transactions = json.loads(res_text.strip())
        return transactions
        
    except Exception as e:
        raise ValueError(f"Failed to extract PDF: {str(e)}")
