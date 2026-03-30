import django_tables2 as tables
from django_tables2.utils import A

from .models import LabelGroup


class LabelGroupTable(tables.Table):
    name = tables.LinkColumn(
        viewname="label_group_detail",
        args=[A("pk")],
        attrs={"td": {"class": "w-100"}},
    )

    class Meta:
        model = LabelGroup
        fields = ("name", "is_active", "order")
        order_by = ("order", "name")
        empty_text = "No label groups found"
        attrs = {"class": "table table-bordered table-hover"}
