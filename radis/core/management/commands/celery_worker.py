from django.conf import settings

from adit_radis_shared.common.management.base.celery_worker import CeleryWorkerCommand


class Command(CeleryWorkerCommand):
    project = "radis"
    paths_to_watch = [settings.BASE_DIR / "radis"]
