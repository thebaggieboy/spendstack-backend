from datetime import timedelta

from django.db.models import F, Sum
from django.utils import timezone

from transactions.models import Transaction


def generate_financial_advice(user_id):
    """Return factual spending summaries without making an external model request."""
    today = timezone.localdate()
    start_date = today - timedelta(days=29)
    expenses = Transaction.objects.filter(
        user_id=user_id,
        date__gte=start_date,
        date__lte=today,
        amount__lt=0,
        is_transfer=False,
    )
    spending_by_category = list(
        expenses.values(name=F("category__name"))
        .annotate(total=Sum("amount"))
        .order_by("total")[:2]
    )

    insights = []
    for item in spending_by_category:
        category = item["name"] or "Uncategorized"
        amount = abs(item["total"])
        insights.append({
            "type": "alert",
            "title": f"Spending in {category}",
            "message": (
                f"You spent ₦{amount:,.2f} in {category} during the last 30 days. "
                "Review the transactions to see whether they match your plans."
            ),
        })

    if not insights:
        return []
    return insights
