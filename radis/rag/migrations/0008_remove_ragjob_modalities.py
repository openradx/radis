# Generated by Django 5.0.4 on 2024-04-10 21:13

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('rag', '0007_ragjob_language'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ragjob',
            name='modalities',
        ),
    ]
