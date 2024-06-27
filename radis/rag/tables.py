import django_tables2 as tables
from django.utils.html import format_html

from radis.core.tables import AnalysisJobTable, AnalysisTaskTable, RecordIdColumn
from radis.rag.templatetags.rag_extras import result_badge_css_class

from .models import RagJob, RagReportInstance, RagTask


class RagJobTable(AnalysisJobTable):
    class Meta(AnalysisJobTable.Meta):
        model = RagJob


class RagTaskTable(AnalysisTaskTable):
    class Meta(AnalysisTaskTable.Meta):
        model = RagTask
        empty_text = "No RAG tasks to show"
        fields = ("id", "status", "message", "ended_at")


class RagReportInstanceTable(tables.Table):
    id = RecordIdColumn(verbose_name="Rag Report Instance ID")
    overall_result = tables.Column(verbose_name="Overall Result")

    class Meta:
        model = RagReportInstance
        empty_text = "No RAG report instances to show"
        fields = ("id", "overall_result")
        attrs = {"class": "table table-bordered table-hover"}

    def render_overall_result(self, value: str, record: RagReportInstance):
        if not record.overall_result:
            return "–"
        css_class = result_badge_css_class(record.overall_result)
        return format_html(f'<span class="badge {css_class}">{value}</span>')
