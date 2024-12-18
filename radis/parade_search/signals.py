from django.db.models.signals import post_save
from django.dispatch import receiver

from ..reports.models import Report  # Import Report from reports app
from .models import ParadeDBReport  # Import ParadeDBReport from current app


@receiver(post_save, sender=Report)
def create_parade_db_report(sender, instance, created, **kwargs):
    if created:  # Only act on newly created Report instances
        # Determine the field name based on the Report's language code
        language_code = instance.language.code
        body_field_name = f"body_{language_code}"

        # Create the ParadeDBReport instance
        parade_db_report = ParadeDBReport(report=instance)

        # Dynamically set the appropriate body field
        if hasattr(parade_db_report, body_field_name):
            setattr(parade_db_report, body_field_name, instance.body)

        # Save the ParadeDBReport instance
        parade_db_report.save()
