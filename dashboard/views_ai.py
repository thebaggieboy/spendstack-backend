from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from analysis.utils.advisor import generate_financial_advice

class AIAdvisorView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user_id = request.user.id
        insights = generate_financial_advice(user_id)
        return Response(insights)
