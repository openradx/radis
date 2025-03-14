import django_filters
from adit_radis_shared.common.forms import SingleFilterFieldFormHelper
from django.http import HttpRequest

from .models import Subscription


class SubscriptionFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="search")
    request: HttpRequest

    class Meta:
        model = Subscription
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form.helper = SingleFilterFieldFormHelper(self.request.GET, "name")
