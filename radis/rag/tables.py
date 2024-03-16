from django.utils.html import format_html

from radis.core.tables import AnalysisJobTable, AnalysisTaskTable
from radis.rag.templatetags.rag_extras import result_badge_css_class

from .models import RagJob, RagTask


class RagJobTable(AnalysisJobTable):
    class Meta(AnalysisJobTable.Meta):
        model = RagJob


class RagTaskTable(AnalysisTaskTable):
    class Meta(AnalysisTaskTable.Meta):
        model = RagTask
        empty_text = "No RAG tasks to show"
        fields = ("id", "status", "message", "ended_at", "overall_result")

    def render_overall_result(self, value: str, record: RagTask):
        if not record.overall_result:
            return "—"
        css_class = result_badge_css_class(record.overall_result)
        return format_html(f'<span class="badge {css_class}">{value}</span>')
