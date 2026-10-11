from django.conf import settings
from django.db import models


class PageVisit(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="page_visits",
    )
    visitor_id = models.UUIDField(db_index=True)
    path = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="analytics_user_visit_idx"),
            models.Index(fields=["path", "created_at"], name="analytics_path_visit_idx"),
        ]

    def __str__(self):
        return f"{self.path} at {self.created_at}"
