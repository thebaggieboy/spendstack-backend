from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from .utils.parser import parse_csv_statement, parse_pdf_statement
from .utils.ai_categorizer import categorize_transactions
from transactions.models import Transaction, Account, Category
from .models import Statement
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
            # Mock grabbing an account for this prototype
            account, _ = Account.objects.get_or_create(
                name="Default Checking", 
                defaults={"user": user} # Hacky fallback for prototype
            )
            
            statement = Statement.objects.create(
                user=user,
                filename=file_obj.name,
            )

            created_transactions = []
            for tx in categorized_data:
                # Ensure category exists dynamically
                cat_name = tx.get('category_name', 'Other')
                cat, _ = Category.objects.get_or_create(
                    name=cat_name, 
                    defaults={"color": "#9ca3af", "is_system": False}
                )
                
                try:
                    obj = Transaction.objects.create(
                        user=user, 
                        account=account,
                        statement=statement,
                        date=tx['date'],
                        amount=tx['amount'],
                        merchant_name=tx.get('merchant_name', 'Unknown'),
                        raw_description=tx['raw_description'],
                        category=cat
                    )
                    created_transactions.append(obj.id)
                except Exception as e:
                    print(f"Failed saving tx: {e}")

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
