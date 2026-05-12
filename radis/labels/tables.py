import django_tables2 as tables
from django_tables2.utils import A

from .models import QuestionSet


class QuestionSetTable(tables.Table):
    name = tables.LinkColumn(
        viewname="question_set_detail",
        args=[A("pk")],
        attrs={"td": {"class": "w-100"}},
    )

    class Meta:
        model = QuestionSet
        fields = ("name", "is_active", "order")
        order_by = ("order", "name")
        empty_text = "No question sets found"
        attrs = {"class": "table table-bordered table-hover"}
