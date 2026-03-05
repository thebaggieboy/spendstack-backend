from rest_framework.views import APIView
from rest_framework.response import Response
from django.db import models
from django.db.models import Sum, Count, F, Min
from django.db.models.functions import TruncDate
from datetime import timedelta
from django.utils import timezone
from transactions.models import Transaction, Category

class DashboardOverviewView(APIView):
    def get(self, request):
        user_id = 1 if not request.user.is_authenticated else request.user.id

        txs = Transaction.objects.filter(user_id=user_id)

        income = txs.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or 0
        expenses = txs.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or 0

        # In a real app we'd track total balance properly over time
        total_balance = float(income) + float(expenses)

        savings_rate = 0
        if income > 0:
            savings_rate = ((income + expenses) / income) * 100

        # Most money spent in a day
        expenses_qs = txs.filter(amount__lt=0)
        daily_expenses = {}
        for tx in expenses_qs:
            day_str = tx.date.strftime('%b %d, %Y')
            if day_str not in daily_expenses:
                daily_expenses[day_str] = 0.0
            daily_expenses[day_str] += float(abs(tx.amount))
            
        highest_daily_spend = 0
        highest_daily_spend_date = None
        if daily_expenses:
            worst_day = max(daily_expenses.items(), key=lambda x: x[1])
            highest_daily_spend_date = worst_day[0]
            highest_daily_spend = worst_day[1]

        # Who you've sent the most money to
        top_merchants = txs.filter(amount__lt=0).values('merchant_name').annotate(total=Sum('amount')).order_by('total')
        top_payee = None
        top_payee_amount = 0
        if top_merchants.exists():
            worst_merchant = top_merchants.first()
            top_payee = worst_merchant['merchant_name'] or "Unknown"
            top_payee_amount = float(abs(worst_merchant['total']))

        return Response({
            "total_balance": total_balance,
            "income_all_time": float(income),
            "expenses_all_time": float(abs(expenses)),
            "savings_rate": float(savings_rate),
            "highest_daily_spend": highest_daily_spend,
            "highest_daily_spend_date": highest_daily_spend_date,
            "top_payee": top_payee,
            "top_payee_amount": top_payee_amount
        })

class CategoryBreakdownView(APIView):
    def get(self, request):
        user_id = 1 if not request.user.is_authenticated else request.user.id

        breakdown = Transaction.objects.filter(
            user_id=user_id, 
            amount__lt=0
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
    def get(self, request):
        user_id = 1 if not request.user.is_authenticated else request.user.id

        transactions = Transaction.objects.filter(user_id=user_id).order_by('date')
        
        daily_stats = {}
        for tx in transactions:
            day_str = tx.date.strftime('%b %d')
            if day_str not in daily_stats:
                daily_stats[day_str] = {"income": 0.0, "expenses": 0.0}
                
            if tx.amount > 0:
                daily_stats[day_str]["income"] += float(tx.amount)
            else:
                daily_stats[day_str]["expenses"] += float(abs(tx.amount))

        data = []
        for day, stats in daily_stats.items():
            data.append({
                "date": day,
                "income": stats["income"],
                "expenses": stats["expenses"]
            })

        return Response(data)

class TopMerchantsView(APIView):
    def get(self, request):
        user_id = 1 if not request.user.is_authenticated else request.user.id

        merchants = Transaction.objects.filter(
            user_id=user_id,
            amount__lt=0
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
