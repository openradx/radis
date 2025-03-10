import logging

from django.core.management import call_command
from procrastinate.contrib.django import app

logger = logging.getLogger(__name__)


@app.periodic(cron="0 3 * * * ")  # every day at 3am
@app.task
def backup_db(*args, **kwargs):
    call_command("backup_db", "--clean", "-v 2")
