from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from .utils.parser import parse_csv_statement, parse_pdf_statement
from .utils.ai_categorizer import categorize_transactions
from transactions.models import Transaction, Account, Category
from .models import Statement
from transactions.models import TransactionRule
from transactions.services import classify_transaction
import json

class StatementUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        file_obj = request.data.get('file')
        
        if not file_obj:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            # 1. Parse File
            if file_obj.name.endswith('.csv'):
                raw_data = parse_csv_statement(file_obj.read())
            elif file_obj.name.endswith('.pdf'):
                raw_data = parse_pdf_statement(file_obj)
            else:
                return Response({"error": "Unsupported file format. Use CSV or PDF."}, status=status.HTTP_400_BAD_REQUEST)

            # 2. Add indices for AI categorizer
            for i, tx in enumerate(raw_data):
                tx['original_index'] = i

            # 3. AI Categorization
            categorized_data = categorize_transactions(raw_data)
            
            # 4. Save to DB
            user = request.user            
            account, _ = Account.objects.get_or_create(
                name="Default Checking", 
                user=user
            )
            
            statement = Statement.objects.create(
                user=user,
                filename=file_obj.name,
            )

            created_transactions = []
            rules = list(
                TransactionRule.objects.filter(user=user, is_active=True).order_by("-priority", "id")
            )
            for tx in categorized_data:
                base_merchant = tx.get('merchant_name', 'Unknown')
                base_category = tx.get('category_name', 'Other')
                merchant_name, cat_name, is_transfer = classify_transaction(
                    tx.get('raw_description', ''),
                    base_merchant,
                    base_category,
                    rules,
                )
                cat, _ = Category.objects.get_or_create(
                    name=cat_name, 
                    defaults={"color": "#9ca3af", "is_system": cat_name == "Transfers"}
                )
                
                obj = Transaction.objects.create(
                    user=user,
                    account=account,
                    statement=statement,
                    date=tx['date'],
                    amount=tx['amount'],
                    merchant_name=merchant_name,
                    original_merchant_name=base_merchant,
                    original_category_name=base_category,
                    raw_description=tx['raw_description'],
                    category=cat,
                    is_transfer=is_transfer,
                )
                created_transactions.append(obj.id)

            return Response({
                "message": f"Successfully processed {len(created_transactions)} transactions.",
                "status": "success"
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class StatementListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        statements = Statement.objects.filter(user=request.user).order_by('-upload_date')
        data = []
        for stmt in statements:
            tx_count = stmt.transactions.count()
            data.append({
                "id": stmt.id,
                "filename": stmt.filename,
                "upload_date": stmt.upload_date.strftime('%b %d, %Y'),
                "transaction_count": tx_count
            })
            
        return Response(data)


class StatementDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.db.models import Sum, Count, Q
        from django.shortcuts import get_object_or_404

        stmt = get_object_or_404(Statement, pk=pk, user=request.user)
        transactions = stmt.transactions.select_related('category').order_by('date', 'id')

        # Aggregate stats
        financial_transactions = transactions.filter(is_transfer=False)
        income = financial_transactions.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or 0
        expenses = financial_transactions.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or 0
        merchant_count = financial_transactions.values('merchant_name').distinct().count()

        # Build running balance timeline
        tx_list = []
        running_balance = 0.0
        for tx in transactions:
            running_balance += float(tx.amount)
            tx_list.append({
                "id": tx.id,
                "date": tx.date.strftime('%Y-%m-%d'),
                "date_display": tx.date.strftime('%b %d'),
                "raw_description": tx.raw_description,
                "merchant_name": tx.merchant_name or tx.raw_description[:40],
                "amount": float(tx.amount),
                "category": tx.category.name if tx.category else "Uncategorized",
                "category_color": tx.category.color if tx.category else "#9ca3af",
                "running_balance": round(running_balance, 2),
                "is_transfer": tx.is_transfer,
            })

        # Category breakdown (expenses only)
        category_data = []
        from django.db.models import F
        cat_qs = financial_transactions.filter(amount__lt=0).values(
            name=F('category__name'),
            color=F('category__color')
        ).annotate(total=Sum('amount')).order_by('total')
        for c in cat_qs:
            category_data.append({
                "name": c['name'] or "Uncategorized",
                "value": round(float(abs(c['total'])), 2),
                "color": c['color'] or "#9ca3af",
            })

        # Daily spending aggregation
        daily_map: dict = {}
        for tx in tx_list:
            if tx['is_transfer']:
                continue
            d = tx['date_display']
            if d not in daily_map:
                daily_map[d] = {"date": d, "income": 0.0, "expenses": 0.0}
            if tx['amount'] > 0:
                daily_map[d]['income'] += tx['amount']
            else:
                daily_map[d]['expenses'] += abs(tx['amount'])
        daily_data = list(daily_map.values())

        return Response({
            "id": stmt.id,
            "filename": stmt.filename,
            "upload_date": stmt.upload_date.strftime('%B %d, %Y'),
            "stats": {
                "total_income": round(float(income), 2),
                "total_expenses": round(float(abs(expenses)), 2),
                "net": round(float(income) + float(expenses), 2),
                "transaction_count": transactions.count(),
                "merchant_count": merchant_count,
            },
            "transactions": tx_list,
            "category_breakdown": category_data,
            "daily_data": daily_data,
        })
