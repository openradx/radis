import logging

from adit_radis_shared.common.models import AppSettings

logger = logging.getLogger(__name__)


class SearchAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Search app settings"
