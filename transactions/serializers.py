from rest_framework import serializers

from .models import TransactionRule


class TransactionRuleSerializer(serializers.ModelSerializer):
    user = serializers.HiddenField(default=serializers.CurrentUserDefault())

    class Meta:
        model = TransactionRule
        fields = [
            "user",
            "id",
            "match_text",
            "match_field",
            "merchant_name",
            "category_name",
            "transfer_action",
            "priority",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_match_text(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter text to match.")
        return value

    def validate(self, attrs):
        existing = self.instance
        merchant_name = attrs.get("merchant_name", existing.merchant_name if existing else "")
        category_name = attrs.get("category_name", existing.category_name if existing else "")
        transfer_action = attrs.get(
            "transfer_action",
            existing.transfer_action if existing else TransactionRule.TRANSFER_AUTO,
        )
        if not merchant_name and not category_name and transfer_action == TransactionRule.TRANSFER_AUTO:
            raise serializers.ValidationError(
                "A rule must rename a merchant, set a category, or override transfer detection."
            )
        return attrs


class SpendingAlertSerializer(serializers.ModelSerializer):
    user = serializers.HiddenField(default=serializers.CurrentUserDefault())
    current_spend = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_exceeded = serializers.BooleanField(read_only=True)

    class Meta:
        from .models import SpendingAlert

        model = SpendingAlert
        fields = [
            "user",
            "id",
            "name",
            "category_name",
            "threshold",
            "period",
            "is_active",
            "current_spend",
            "is_exceeded",
            "created_at",
        ]
        read_only_fields = ["id", "current_spend", "is_exceeded", "created_at"]

    def validate_threshold(self, value):
        if value <= 0:
            raise serializers.ValidationError("Threshold must be greater than zero.")
        return value

    def validate_category_name(self, value):
        return value.strip()
