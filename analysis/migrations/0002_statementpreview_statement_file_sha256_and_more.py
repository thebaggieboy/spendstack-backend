import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="statement",
            name="file_sha256",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.CreateModel(
            name="StatementPreview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=255)),
                ("file_sha256", models.CharField(max_length=64)),
                ("transactions", models.JSONField()),
                ("warnings", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="statement_previews",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="statementpreview",
            index=models.Index(fields=["user", "expires_at"], name="analysis_st_user_id_f4dfe2_idx"),
        ),
        migrations.AddConstraint(
            model_name="statement",
            constraint=models.UniqueConstraint(fields=("user", "file_sha256"), name="unique_user_statement_file_sha256"),
        ),
    ]
