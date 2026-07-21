import django.db.models.deletion
from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0010_remove_subscription_provider"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        # Filter results replace the old per-question answers; existing
        # Questions are dropped in favor of FilterQuestions with an expected
        # answer.
        migrations.RenameField(
            model_name="subscribeditem",
            old_name="answers",
            new_name="filter_results",
        ),
        migrations.AddField(
            model_name="subscribeditem",
            name="extraction_results",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="FilterQuestion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("question", models.CharField(max_length=300)),
                (
                    "expected_answer",
                    models.CharField(
                        choices=[("Y", "Yes"), ("N", "No")], default="Y", max_length=1
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="filter_questions",
                        to="subscriptions.subscription",
                    ),
                ),
            ],
        ),
        migrations.DeleteModel(
            name="Question",
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptionjob"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptionjob", reverse=True
            ),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptiontask"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptiontask", reverse=True
            ),
        ),
        migrations.AddField(
            model_name="subscription",
            name="last_viewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="query",
        ),
    ]
