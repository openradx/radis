from adit_radis_shared.common.mixins import LockedMixin

from .apps import SECTION_NAME
from .models import ExtractionsAppSettings


class ExtractionsLockedMixin(LockedMixin):
    settings_model = ExtractionsAppSettings
    section_name = SECTION_NAME
