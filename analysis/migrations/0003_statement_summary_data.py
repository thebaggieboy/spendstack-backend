from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0002_statementpreview_statement_file_sha256_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="statement",
            name="summary_data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="statementpreview",
            name="summary_data",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
