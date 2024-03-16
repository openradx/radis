import logging

from radis.core.models import AppSettings

logger = logging.getLogger(__name__)


class SearchAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Search app settings"
