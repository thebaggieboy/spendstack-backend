from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Avg, Max, Min, Q, F
from django.db.models.functions import TruncMonth, ExtractWeekDay
from django.utils import timezone
from billing.permissions import HasActiveProSubscription
import calendar
from collections import defaultdict
from datetime import date
from transactions.models import Transaction


WEEKDAY_NAMES = {
    1: 'Sunday', 2: 'Monday', 3: 'Tuesday',
    4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday',
}


class SpendingInsightsView(APIView):
    """
    Returns key spending stats derived from the user's full transaction history:
    - avg_daily_spend       : average amount spent per day that had any spending
    - avg_transaction_value : mean absolute value of all expense transactions
    - busiest_spend_day     : day-of-week with the highest cumulative spending
    - largest_expense       : single biggest outgoing transaction
    - largest_expense_name  : merchant/description of that transaction
    - largest_expense_date  : date of that transaction
    - total_txn_count       : total number of transactions recorded
    - income_txn_count      : how many credit (income) transactions
    - expense_txn_count     : how many debit (expense) transactions
    - unique_merchants      : number of distinct merchants/payees
    - no_spend_days         : count of calendar days between first and last txn
                              that had zero spending activity
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        txs = Transaction.objects.filter(user_id=user_id, is_transfer=False)

        if not txs.exists():
            return Response({})

        expense_qs = txs.filter(amount__lt=0)
        income_qs = txs.filter(amount__gt=0)

        # ── Basic counts ────────────────────────────────────────────────────
        total_txn_count = txs.count()
        expense_txn_count = expense_qs.count()
        income_txn_count = income_qs.count()

        # ── Avg daily spend ─────────────────────────────────────────────────
        # Group expenses by date → sum per day → average of those sums
        daily = defaultdict(float)
        for tx in expense_qs.values('date', 'amount'):
            daily[tx['date']] += float(abs(tx['amount']))
        avg_daily_spend = (sum(daily.values()) / len(daily)) if daily else 0

        # ── Avg single transaction value ─────────────────────────────────────
        avg_txn_agg = expense_qs.aggregate(avg=Avg('amount'))
        avg_transaction_value = float(abs(avg_txn_agg['avg'])) if avg_txn_agg['avg'] else 0

        # ── Busiest day of week ─────────────────────────────────────────────
        by_weekday = (
            expense_qs
            .annotate(wd=ExtractWeekDay('date'))
            .values('wd')
            .annotate(total=Sum('amount'))
            .order_by('total')  # most negative = biggest spend
        )
        busiest_spend_day = None
        if by_weekday:
            wd_num = by_weekday.first()['wd']
            busiest_spend_day = WEEKDAY_NAMES.get(wd_num, 'Unknown')

        # ── Largest single expense ───────────────────────────────────────────
        largest_tx = expense_qs.order_by('amount').first()
        largest_expense = 0
        largest_expense_name = None
        largest_expense_date = None
        if largest_tx:
            largest_expense = float(abs(largest_tx.amount))
            largest_expense_name = largest_tx.merchant_name or largest_tx.raw_description[:40]
            largest_expense_date = largest_tx.date.strftime('%b %d, %Y')

        # ── Unique merchants ────────────────────────────────────────────────
        unique_merchants = (
            expense_qs
            .exclude(merchant_name='')
            .values('merchant_name')
            .distinct()
            .count()
        )

        # ── No-spend days ────────────────────────────────────────────────────
        date_range = txs.aggregate(first=Min('date'), last=Max('date'))
        no_spend_days = 0
        if date_range['first'] and date_range['last']:
            from datetime import timedelta
            total_days = (date_range['last'] - date_range['first']).days + 1
            days_with_spend = len(daily)
            no_spend_days = max(0, total_days - days_with_spend)

        return Response({
            'avg_daily_spend': round(avg_daily_spend, 2),
            'avg_transaction_value': round(avg_transaction_value, 2),
            'busiest_spend_day': busiest_spend_day,
            'largest_expense': round(largest_expense, 2),
            'largest_expense_name': largest_expense_name,
            'largest_expense_date': largest_expense_date,
            'total_txn_count': total_txn_count,
            'income_txn_count': income_txn_count,
            'expense_txn_count': expense_txn_count,
            'unique_merchants': unique_merchants,
            'no_spend_days': no_spend_days,
        })


class MonthlyTrendView(APIView):
    """
    Returns income and expenses aggregated by calendar month,
    ordered chronologically. Used for the monthly bar chart.

    Each item: { month: "Jan 2025", income: float, expenses: float, net: float }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        txs = Transaction.objects.filter(user_id=user_id, is_transfer=False)

        if not txs.exists():
            return Response([])

        months = (
            txs
            .annotate(month=TruncMonth('date'))
            .values('month')
            .annotate(
                income=Sum('amount', filter=Q(amount__gt=0)),
                expenses=Sum('amount', filter=Q(amount__lt=0)),
            )
            .order_by('month')
        )

        data = []
        for row in months:
            inc = float(row['income'] or 0)
            exp = float(abs(row['expenses'] or 0))
            data.append({
                'month': row['month'].strftime('%b %Y'),
                'income': round(inc, 2),
                'expenses': round(exp, 2),
                'net': round(inc - exp, 2),
            })

        return Response(data)


class MonthlyComparisonView(APIView):
    permission_classes = [IsAuthenticated, HasActiveProSubscription]

    def get(self, request):
        today = timezone.localdate()
        current_start = today.replace(day=1)
        previous_month = current_start.replace(day=1)
        if previous_month.month == 1:
            previous_month = previous_month.replace(year=previous_month.year - 1, month=12)
        else:
            previous_month = previous_month.replace(month=previous_month.month - 1)
        previous_end = previous_month.replace(
            day=min(today.day, calendar.monthrange(previous_month.year, previous_month.month)[1])
        )

        def summarize(start, end):
            period_transactions = Transaction.objects.filter(
                user=request.user,
                is_transfer=False,
                date__gte=start,
                date__lte=end,
            )
            totals = period_transactions.aggregate(
                income=Sum("amount", filter=Q(amount__gt=0)),
                expenses=Sum("amount", filter=Q(amount__lt=0)),
            )
            categories = period_transactions.filter(amount__lt=0).values(
                category_name=F("category__name")
            ).annotate(total=Sum("amount"))
            return (
                float(abs(totals["expenses"] or 0)),
                float(totals["income"] or 0),
                {
                    row["category_name"] or "Uncategorized": float(abs(row["total"] or 0))
                    for row in categories
                },
            )

        current_expenses, current_income, current_categories = summarize(current_start, today)
        previous_expenses, previous_income, previous_categories = summarize(previous_month, previous_end)

        def change(current, previous):
            return {
                "amount": round(current - previous, 2),
                "percent": round((current - previous) / previous * 100, 1) if previous else None,
            }

        category_names = set(current_categories) | set(previous_categories)
        category_comparison = [
            {
                "category": name,
                "current": round(current_categories.get(name, 0), 2),
                "previous": round(previous_categories.get(name, 0), 2),
                "change": round(current_categories.get(name, 0) - previous_categories.get(name, 0), 2),
            }
            for name in category_names
        ]
        category_comparison.sort(key=lambda row: abs(row["change"]), reverse=True)

        return Response({
            "current_period": {"start": current_start.isoformat(), "end": today.isoformat()},
            "previous_period": {"start": previous_month.isoformat(), "end": previous_end.isoformat()},
            "expenses": {
                "current": round(current_expenses, 2),
                "previous": round(previous_expenses, 2),
                "change": change(current_expenses, previous_expenses),
            },
            "income": {
                "current": round(current_income, 2),
                "previous": round(previous_income, 2),
                "change": change(current_income, previous_income),
            },
            "categories": category_comparison[:5],
        })
