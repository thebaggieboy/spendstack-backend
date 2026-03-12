from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Transaction

class TransactionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        statement_id = request.query_params.get('statement_id')
        
        queryset = Transaction.objects.filter(user=user).select_related('category')
        
        if statement_id:
            queryset = queryset.filter(statement_id=statement_id)
            
        # Optional: Pagination could be added here
        
        data = [
            {
                "id": t.id,
                "name": t.merchant_name or t.raw_description[:30],
                "amount": float(t.amount),
                "date": t.date.strftime('%b %d, %Y'),
                "category": t.category.name if t.category else 'Uncategorized',
                "status": "completed" if not t.is_pending else "pending"
            }
            for t in queryset
        ]
        
        return Response(data)
