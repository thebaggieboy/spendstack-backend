from django.db import models
from django.conf import settings

class Statement(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='statements')
    filename = models.CharField(max_length=255)
    upload_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.filename} - {self.user.email}"
