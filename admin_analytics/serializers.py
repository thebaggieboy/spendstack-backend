from rest_framework import serializers


class PageVisitSerializer(serializers.Serializer):
    visitor_id = serializers.UUIDField()
    path = serializers.CharField(max_length=255, trim_whitespace=True)

    def validate_path(self, value):
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise serializers.ValidationError("Provide a same-site path without query parameters.")
        return value
