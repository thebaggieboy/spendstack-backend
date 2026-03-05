from rest_framework.views import APIView
from rest_framework.response import Response
from analysis.utils.advisor import generate_financial_advice

class AIAdvisorView(APIView):
    def get(self, request):
        user_id = 1 if not request.user.is_authenticated else request.user.id
        insights = generate_financial_advice(user_id)
        return Response(insights)
