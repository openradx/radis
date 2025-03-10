# Generated by Django 5.1.6 on 2025-02-25 18:27

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0005_alter_subscription_patient_sex"),
    ]

    operations = [
        migrations.CreateModel(
            name="QuestionField",
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
                ("name", models.CharField(max_length=100)),
                ("question", models.CharField(max_length=500)),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="question_fields",
                        to="subscriptions.subscription",
                    ),
                ),
            ],
        ),
        migrations.DeleteModel(
            name="FilterField",
        ),
        migrations.AddConstraint(
            model_name="questionfield",
            constraint=models.UniqueConstraint(
                fields=("name", "subscription_id"),
                name="unique_question_field_name_per_subscription",
            ),
        ),
    ]
