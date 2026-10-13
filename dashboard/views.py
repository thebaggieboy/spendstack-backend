from decimal import Decimal

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Count, F, Q, Sum
from transactions.models import Transaction


def confirmed_non_transfer_transactions(user_id):
    return Transaction.objects.filter(
        user_id=user_id,
        is_transfer=False,
        is_pending=False,
    )


class DashboardOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        txs = confirmed_non_transfer_transactions(request.user.id)
        totals = txs.aggregate(
            credits=Sum("amount", filter=Q(amount__gt=0)),
            debits=Sum("amount", filter=Q(amount__lt=0)),
        )
        credits = totals["credits"] or Decimal("0.00")
        debits = totals["debits"] or Decimal("0.00")
        net_cash_flow = credits + debits
        savings_rate = Decimal("0")
        if credits > 0:
            savings_rate = net_cash_flow / credits * Decimal("100")

        largest_spend_day = (
            txs.filter(amount__lt=0)
            .values("date")
            .annotate(total=Sum("amount"))
            .order_by("total", "date")
            .first()
        )
        highest_daily_spend = Decimal("0.00")
        highest_daily_spend_date = None
        if largest_spend_day:
            highest_daily_spend = abs(largest_spend_day["total"])
            highest_daily_spend_date = largest_spend_day["date"].strftime("%b %d, %Y")

        top_payee_row = (
            txs.filter(amount__lt=0)
            .values("merchant_name")
            .annotate(total=Sum("amount"))
            .order_by("total", "merchant_name")
            .first()
        )
        top_payee = None
        top_payee_amount = Decimal("0.00")
        if top_payee_row:
            top_payee = top_payee_row["merchant_name"] or "Unknown"
            top_payee_amount = abs(top_payee_row["total"])

        return Response({
            "net_cash_flow": float(net_cash_flow),
            "credits_all_time": float(credits),
            "expenses_all_time": float(abs(debits)),
            "savings_rate": float(savings_rate),
            "highest_daily_spend": float(highest_daily_spend),
            "highest_daily_spend_date": highest_daily_spend_date,
            "top_payee": top_payee,
            "top_payee_amount": float(top_payee_amount),
        })


class CategoryBreakdownView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        breakdown = confirmed_non_transfer_transactions(request.user.id).filter(
            amount__lt=0,
        ).values(
            name=F('category__name'),
            color=F('category__color')
        ).annotate(
            value=Sum('amount')
        ).order_by('value')

        data = [
            {
                "name": item['name'] or 'Uncategorized',
                "value": float(abs(item['value'])),
                "color": item['color'] or '#9ca3af'
            }
            for item in breakdown
        ]

        return Response(sorted(data, key=lambda x: x['value'], reverse=True))

class CashFlowView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        transactions = confirmed_non_transfer_transactions(request.user.id).order_by("date")
        daily_stats = {}
        for tx in transactions.only("date", "amount").iterator():
            date_key = tx.date.isoformat()
            if date_key not in daily_stats:
                daily_stats[date_key] = {
                    "income": Decimal("0.00"),
                    "expenses": Decimal("0.00"),
                }

            if tx.amount > 0:
                daily_stats[date_key]["income"] += tx.amount
            else:
                daily_stats[date_key]["expenses"] += abs(tx.amount)

        data = []
        for day, stats in sorted(daily_stats.items()):
            data.append({
                "date": day,
                "income": float(stats["income"]),
                "expenses": float(stats["expenses"]),
            })

        return Response(data)


class TopMerchantsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        merchants = confirmed_non_transfer_transactions(request.user.id).filter(
            amount__lt=0,
        ).values(
            'merchant_name', 
            cat_name=F('category__name')
        ).annotate(
            amount=Sum('amount'),
            count=Count('id')
        ).order_by('amount')[:5]

        data = [
            {
                "name": m['merchant_name'],
                "amount": float(abs(m['amount'])),
                "count": m['count'],
                "category": m['cat_name'] or 'Uncategorized',
                "logo": m['merchant_name'][:2].upper() if m['merchant_name'] else '??'
            }
            for m in merchants
        ]

        return Response(data)
