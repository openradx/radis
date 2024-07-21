import logging

from adit_radis_shared.accounts.models import User
from django.conf import settings
from django.core.mail import send_mail
from django.core.management import call_command
from procrastinate.contrib.django import app

logger = logging.getLogger(__name__)


@app.task
def broadcast_mail(subject: str, message: str):
    recipients = []
    for user in User.objects.all():
        if user.email:
            recipients.append(user.email)

    send_mail(subject, message, settings.SUPPORT_EMAIL, recipients)

    logger.info("Successfully sent an Email to %d recipients.", len(recipients))


@app.periodic(cron="0 3 * * * ")  # every day at 3am
@app.task
def backup_db(*args, **kwargs):
    call_command("backup_db")
