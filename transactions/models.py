from django.db import models
from django.conf import settings

class Account(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='accounts')
    name = models.CharField(max_length=100)
    bank_name = models.CharField(max_length=100, blank=True)
    mask = models.CharField(max_length=4, blank=True)

    def __str__(self):
        return f"{self.name} - {self.user.username}"


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    color = models.CharField(max_length=20, default="#8b5cf6") # For frontend UI
    is_system = models.BooleanField(default=True) # True for default categories
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Transaction(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transactions')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    
    date = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2) 
    # Convention: negative for expense, positive for income
    
    merchant_name = models.CharField(max_length=255, blank=True)
    raw_description = models.CharField(max_length=500)
    
    is_pending = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.date} - {self.merchant_name or self.raw_description[:20]} - {self.amount}"
