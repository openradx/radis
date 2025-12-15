import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
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

    def render_num_reports(self, value, record):
        """Render the num_reports column with a notification badge for new reports."""
        num_new = getattr(record, "num_new_reports", 0)
        url = reverse("subscription_inbox", args=[record.pk])

        if num_new > 0:
            return format_html(
                '<a href="{}">{}<span class="badge bg-primary ms-2">{} new</span></a>',
                url,
                value,
                num_new,
            )
        return format_html('<a href="{}">{}</a>', url, value)

    class Meta:
        model = Subscription
        fields = ("name", "num_reports")
        order_by = ("name",)
        empty_text = "No subscriptions found"
        attrs = {
            "class": "table table-bordered table-hover",
        }
