# Generated by Django 4.2.11 on 2024-03-29 09:53

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_UPDATE_SITE_NAME'),
    ]

    operations = [
        migrations.DeleteModel(
            name='CoreSettings',
        ),
    ]
