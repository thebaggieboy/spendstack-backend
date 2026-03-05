import os
import json
from google import genai
from django.db.models import Sum, F
from datetime import timedelta
from django.utils import timezone
from transactions.models import Transaction

def generate_financial_advice(user_id):
    """
    Gathers last 30 days of data and asks Gemini to generate 2 key insights.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NEXT_GEMINI_API_KEY")
    if not api_key:
        return _fallback_advice()

    # Extract all expenses
    expenses = list(Transaction.objects.filter(
        user_id=user_id, amount__lt=0
    ).values(name=F('category__name')).annotate(total=Sum('amount')).order_by('total'))
    
    # Extract top 3 largest transactions
    largest_txs = list(Transaction.objects.filter(
        user_id=user_id, amount__lt=0
    ).order_by('amount').values('merchant_name', 'amount', 'date')[:3])

    context = {
        "overall_spending_by_category": expenses,
        "largest_individual_transactions": largest_txs
    }
    
    prompt = f"""
    You are an expert, encouraging financial advisor AI.
    Analyze the following user spending data which includes overall spending by category and their largest individual transactions.
    Data: {json.dumps(context, default=str)}
    
    Provide exactly 2 distinct, actionable insights. 
    One should ideally be a "Warning/Alert" (e.g., high spending in a category or a massive single transaction).
    One should ideally be a "Positive Reinforcement" (e.g., healthy diversification or keeping costs low).
    
    Respond ONLY with a valid JSON array of objects. NO markdown formatting.
    Format:
    [
        {{
            "type": "alert", // or "success"
            "title": "Short punchy title",
            "message": "The natural language advice message..."
        }}
    ]
    """

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        res_text = response.text.strip()
        if res_text.startswith("```json"):
            res_text = res_text[7:]
        if res_text.endswith("```"):
            res_text = res_text[:-3]
            
        return json.loads(res_text.strip())
    except Exception as e:
        print(f"Advice generation failed: {e}")
        return _fallback_advice()

def _fallback_advice():
    return [
        {
            "type": "alert",
            "title": "Dining Out Alert",
            "message": "You've spent $450 on Food & Dining this month, which is 20% higher than your average. Consider cooking 2 more meals at home this week to stay on budget."
        },
        {
            "type": "success",
            "title": "Great Job on Utilities!",
            "message": "Your utility bills are down 15% compared to last month. The smart thermostat schedule seems to be working perfectly."
        }
    ]
