from adit_radis_shared.common.management.base.celery_worker import CeleryWorkerCommand
from django.conf import settings


class Command(CeleryWorkerCommand):
    project = "radis"
    paths_to_watch = [settings.BASE_DIR / "radis"]
