from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from transactions.models import Transaction

class DashboardTransactionsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user_id = request.user.id
        
        # Get 10 most recent transactions
        transactions = Transaction.objects.filter(
            user_id=user_id
        ).select_related('category').order_by('-date', '-created_at')[:10]
        
        data = [
            {
                "id": t.id,
                "name": t.merchant_name or t.raw_description[:30],
                "amount": float(t.amount),
                "date": t.date.strftime('%b %d, %Y'),
                "category": t.category.name if t.category else 'Uncategorized',
                "status": "completed" if not t.is_pending else "pending"
            }
            for t in transactions
        ]
        
        return Response(data)
