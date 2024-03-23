from adit_radis_shared.common.mixins import LockedMixin

from .apps import SECTION_NAME
from .models import RagAppSettings


class RagLockedMixin(LockedMixin):
    settings_model = RagAppSettings
    section_name = SECTION_NAME
