from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta

from django.db.models import Sum, Count, Avg, Min, Max, Q, F
from django.db.models.functions import TruncMonth, ExtractWeekDay
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from collections import defaultdict
import calendar
from transactions.models import Transaction
from .views import confirmed_non_transfer_transactions


CENT = Decimal("0.01")
TENTH = Decimal("0.1")


def _round_decimal(value, quantum):
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


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
        all_confirmed = Transaction.objects.filter(user_id=user_id, is_pending=False)
        txs = confirmed_non_transfer_transactions(user_id)
        period = request.query_params.get("period", "all")
        today = timezone.localdate()
        period_starts = {
            "month": today.replace(day=1),
            "90days": today - timedelta(days=89),
            "year": today.replace(month=1, day=1),
        }
        if period not in {"all", *period_starts}:
            return Response(
                {"detail": "Choose a valid insights period."},
                status=400,
            )
        if period in period_starts:
            txs = txs.filter(date__gte=period_starts[period], date__lte=today)
            all_confirmed = all_confirmed.filter(
                date__gte=period_starts[period],
                date__lte=today,
            )

        expense_qs = txs.filter(amount__lt=0)
        income_qs = txs.filter(amount__gt=0)

        # ── Basic counts ────────────────────────────────────────────────────
        expense_txn_count = expense_qs.count()
        income_txn_count = income_qs.count()
        total_txn_count = expense_txn_count + income_txn_count

        # ── Avg daily spend ─────────────────────────────────────────────────
        # Group expenses by date → sum per day → average of those sums
        daily = defaultdict(lambda: Decimal("0.00"))
        for tx in expense_qs.values('date', 'amount'):
            daily[tx['date']] += abs(tx['amount'])
        avg_daily_spend = (
            sum(daily.values(), Decimal("0.00")) / len(daily)
            if daily
            else Decimal("0.00")
        )

        # ── Avg single transaction value ─────────────────────────────────────
        avg_txn_agg = expense_qs.aggregate(avg=Avg('amount'))
        avg_transaction_value = abs(avg_txn_agg['avg']) if avg_txn_agg['avg'] else Decimal("0.00")
        total_expenses = abs(
            expense_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        )

        # ── Busiest day of week ─────────────────────────────────────────────
        by_weekday = (
            expense_qs
            .annotate(wd=ExtractWeekDay('date'))
            .values('wd')
            .annotate(total=Sum('amount'))
            .order_by('total', 'wd')
        )
        busiest_spend_day = None
        if by_weekday:
            wd_num = by_weekday.first()['wd']
            busiest_spend_day = WEEKDAY_NAMES.get(wd_num, 'Unknown')

        # ── Largest single expense ───────────────────────────────────────────
        largest_tx = expense_qs.order_by('amount').first()
        largest_expense = Decimal("0.00")
        largest_expense_name = None
        largest_expense_date = None
        if largest_tx:
            largest_expense = abs(largest_tx.amount)
            largest_expense_name = largest_tx.merchant_name or largest_tx.raw_description[:40]
            largest_expense_date = largest_tx.date.strftime('%b %d, %Y')

        top_category_row = (
            expense_qs.values(name=F("category__name"))
            .annotate(total=Sum("amount"))
            .order_by("total", "name")
            .first()
        )
        top_category = (
            top_category_row["name"] or "Uncategorized"
            if top_category_row else None
        )
        top_category_amount = (
            abs(top_category_row["total"])
            if top_category_row else Decimal("0.00")
        )
        top_category_share = (
            top_category_amount / total_expenses * Decimal("100")
            if total_expenses else Decimal("0.0")
        )

        top_recipient_row = (
            all_confirmed.filter(is_transfer=True, amount__lt=0)
            .exclude(merchant_name="")
            .values("merchant_name")
            .annotate(count=Count("id"), total=Sum("amount"))
            .order_by("-count", "total", "merchant_name")
            .first()
        )
        top_recipient = top_recipient_row["merchant_name"] if top_recipient_row else None
        top_recipient_count = top_recipient_row["count"] if top_recipient_row else 0
        top_recipient_amount = (
            abs(top_recipient_row["total"])
            if top_recipient_row else Decimal("0.00")
        )

        # ── Unique merchants ────────────────────────────────────────────────
        unique_merchants = (
            expense_qs
            .exclude(merchant_name='')
            .values('merchant_name')
            .distinct()
            .count()
        )

        # ── No-spend days ────────────────────────────────────────────────────
        date_range = txs.exclude(amount=0).aggregate(first=Min('date'), last=Max('date'))
        no_spend_days = 0
        if date_range['first'] and date_range['last']:
            total_days = (date_range['last'] - date_range['first']).days + 1
            days_with_spend = len(daily)
            no_spend_days = max(0, total_days - days_with_spend)

        return Response({
            'has_data': total_txn_count > 0 or top_recipient_count > 0,
            'period': period,
            'period_start': period_starts.get(period).isoformat() if period in period_starts else None,
            'period_end': today.isoformat() if period in period_starts else None,
            'avg_daily_spend': float(_round_decimal(avg_daily_spend, CENT)),
            'avg_transaction_value': float(_round_decimal(avg_transaction_value, CENT)),
            'busiest_spend_day': busiest_spend_day,
            'largest_expense': float(_round_decimal(largest_expense, CENT)),
            'largest_expense_name': largest_expense_name,
            'largest_expense_date': largest_expense_date,
            'total_txn_count': total_txn_count,
            'income_txn_count': income_txn_count,
            'expense_txn_count': expense_txn_count,
            'unique_merchants': unique_merchants,
            'no_spend_days': no_spend_days,
            'top_category': top_category,
            'top_category_amount': float(_round_decimal(top_category_amount, CENT)),
            'top_category_share_percent': float(_round_decimal(top_category_share, TENTH)),
            'top_recipient': top_recipient,
            'top_recipient_transfer_count': top_recipient_count,
            'top_recipient_transfer_amount': float(_round_decimal(top_recipient_amount, CENT)),
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
        txs = confirmed_non_transfer_transactions(user_id)

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
            credits = row['income'] or Decimal("0.00")
            expenses = abs(row['expenses'] or Decimal("0.00"))
            data.append({
                'month': row['month'].strftime('%b %Y'),
                'income': float(_round_decimal(credits, CENT)),
                'expenses': float(_round_decimal(expenses, CENT)),
                'net': float(_round_decimal(credits - expenses, CENT)),
            })

        return Response(data)


class MonthlyComparisonView(APIView):
    permission_classes = [IsAuthenticated]

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
            period_transactions = confirmed_non_transfer_transactions(
                request.user.id,
            ).filter(
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
                abs(totals["expenses"] or Decimal("0.00")),
                totals["income"] or Decimal("0.00"),
                {
                    row["category_name"] or "Uncategorized": abs(row["total"] or Decimal("0.00"))
                    for row in categories
                },
            )

        current_expenses, current_income, current_categories = summarize(current_start, today)
        previous_expenses, previous_income, previous_categories = summarize(previous_month, previous_end)

        def change(current, previous):
            amount = _round_decimal(current - previous, CENT)
            return {
                "amount": float(amount),
                "percent": (
                    float(_round_decimal(amount / previous * Decimal("100"), TENTH))
                    if previous
                    else None
                ),
            }

        category_names = set(current_categories) | set(previous_categories)
        category_comparison = [
            {
                "category": name,
                "current": float(_round_decimal(current_categories.get(name, Decimal("0.00")), CENT)),
                "previous": float(_round_decimal(previous_categories.get(name, Decimal("0.00")), CENT)),
                "change": float(_round_decimal(
                    current_categories.get(name, Decimal("0.00"))
                    - previous_categories.get(name, Decimal("0.00")),
                    CENT,
                )),
            }
            for name in category_names
        ]
        category_comparison.sort(key=lambda row: abs(row["change"]), reverse=True)

        return Response({
            "current_period": {"start": current_start.isoformat(), "end": today.isoformat()},
            "previous_period": {"start": previous_month.isoformat(), "end": previous_end.isoformat()},
            "expenses": {
                "current": float(_round_decimal(current_expenses, CENT)),
                "previous": float(_round_decimal(previous_expenses, CENT)),
                "change": change(current_expenses, previous_expenses),
            },
            "income": {
                "current": float(_round_decimal(current_income, CENT)),
                "previous": float(_round_decimal(previous_income, CENT)),
                "change": change(current_income, previous_income),
            },
            "categories": category_comparison[:5],
        })
