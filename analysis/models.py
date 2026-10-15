from django.db import models
from django.conf import settings
from django.utils import timezone

class Statement(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='statements')
    filename = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64, null=True, blank=True)
    summary_data = models.JSONField(default=dict, blank=True)
    upload_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "file_sha256"],
                name="unique_user_statement_file_sha256",
            ),
        ]
    
    def __str__(self):
        return f"{self.filename} - {self.user.email}"


class StatementPreview(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="statement_previews",
    )
    filename = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64)
    transactions = models.JSONField()
    summary_data = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "expires_at"],
                name="analysis_st_user_id_f4dfe2_idx",
            )
        ]

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()


class StatementImportAttempt(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="statement_import_attempts",
    )
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=20, default="failed")
    detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "status", "created_at"],
                name="stmt_imp_user_status_idx",
            )
        ]
