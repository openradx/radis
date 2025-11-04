from adit_radis_shared.common.factories import BaseDjangoModelFactory

from .models import SearchAppSettings


class SearchAppSettingsFactory(BaseDjangoModelFactory[SearchAppSettings]):
    class Meta:
        model = SearchAppSettings
