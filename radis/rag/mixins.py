from radis.core.mixins import LockedMixin

from .apps import SECTION_NAME
from .models import RagAppSettings


class RagLockedMixin(LockedMixin):
    settings_model = RagAppSettings
    section_name = SECTION_NAME
