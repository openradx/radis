# Generated by Django 3.0.7 on 2020-06-13 12:51

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('batch_transfer', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='batchtransferrequest',
            name='study_date',
            field=models.DateField(default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
