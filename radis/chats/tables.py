import django_tables2 as tables
from django_tables2.utils import A

from .models import Chat


class ChatTable(tables.Table):
    title = tables.LinkColumn(
        viewname="chat_detail", args=[A("pk")], attrs={"td": {"class": "w-100"}}
    )
    updated_at = tables.DateTimeColumn(
        verbose_name="Last Used", attrs={"td": {"class": "text-nowrap"}}
    )

    class Meta:
        model = Chat
        fields = ("title", "updated_at")
        order_by = ("-updated_at",)
        empty_text = "No chats found"
        attrs = {
            "class": "table table-bordered table-hover",
        }
