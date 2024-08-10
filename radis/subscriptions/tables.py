import django_tables2 as tables
from django_tables2.utils import A

from .models import Subscription


class SubscriptionTable(tables.Table):
    name = tables.LinkColumn(
        viewname="subscription_detail", args=[A("pk")], attrs={"td": {"class": "w-100"}}
    )
    num_reports = tables.LinkColumn(
        viewname="subscription_inbox",
        args=[A("pk")],
        verbose_name="# Reports",
        attrs={
            "th": {"class": "text-nowrap"},
            "td": {"class": "text-center"},
        },
    )

    class Meta:
        model = Subscription
        fields = ("name", "num_reports")
        order_by = ("name",)
        empty_text = "No subscriptions found"
        attrs = {
            "class": "table table-bordered table-hover",
        }
