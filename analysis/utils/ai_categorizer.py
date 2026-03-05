import os
import json
from google import genai
from transactions.models import Category

def categorize_transactions(transactions_list):
    """
    Takes a list of dictionaries: [{"date": "...", "raw_description": "...", "amount": "..."}]
    Uses Google Gemini to return a clean merchant name and a category name.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")
    if not api_key:
        print("WARNING: GEMINI_API_KEY or NEXT_GEMINI_API_KEY not set. Using fallback mock categorization.")
        return _fallback_categorize(transactions_list)

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    You are a precise financial categorization AI.
    I will provide a JSON list of bank transactions.
    
    For each transaction, determine the best generic 'category_name' (e.g., 'Food & Dining', 'Utilities', 'Transportation', 'Income', 'Transfers', 'Bank Charges'), and extract a clean 'merchant_name' from the 'raw_description'.
    
    CRITICAL INSTRUCTIONS FOR NIGERIAN BANK FORMATS:
    - Recognize NIP transfers (e.g., "NIP/TRF/...", "NIP Transfer from..."). Extract the true sender/receiver name as the merchant.
    - Recognize POS transactions (e.g., "POS/PUR/...", "WEB/PUR/...", "Paystack", "Flutterwave", "Remita"). Extract the actual business name.
    - Recognize Bank Charges (e.g., "SMS Alert", "Card Maintenance", "VAT", "Stamp Duty"). Categorize these explicitly (e.g., 'Bank Charges' or 'Fees').
    - If it's an income (positive amount), categorize it as 'Income' or 'Salary' or similar.
    
    Respond ONLY with a valid JSON array of objects, with NO markdown formatting, NO backticks. Focus strictly on returning parsable JSON.
    Format:
    [
        {{
            "original_index": 0,
            "category_name": "Food & Dining",
            "merchant_name": "Clean Business Name"
        }}
    ]
    
    Transactions to process:
    {json.dumps(transactions_list[:50])}  # Limit for prototype
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        # Clean potential markdown from response
        res_text = response.text.strip()
        if res_text.startswith("```json"):
            res_text = res_text[7:]
        if res_text.endswith("```"):
            res_text = res_text[:-3]
            
        ai_results = json.loads(res_text.strip())
        
        # Merge results back
        for res in ai_results:
            idx = res.get('original_index')
            if idx is not None and idx < len(transactions_list):
                transactions_list[idx]['category_name'] = res.get('category_name', 'Other')
                transactions_list[idx]['merchant_name'] = res.get('merchant_name', 'Unknown')
                
        return transactions_list
        
    except Exception as e:
        print(f"AI Categorization Failed: {str(e)}")
        return _fallback_categorize(transactions_list)

def _fallback_categorize(transactions_list):
    """Fallback logic if API key isn't set or call fails"""
    for tx in transactions_list:
        tx['category_name'] = 'Other'
        tx['merchant_name'] = tx['raw_description'][:15].strip()
        
    return transactions_list
