import django_tables2 as tables

from radis.core.tables import AnalysisJobTable, AnalysisTaskTable

from .models import ExtractionInstance, ExtractionJob, ExtractionTask


class ExtractionJobTable(AnalysisJobTable):
    class Meta(AnalysisJobTable.Meta):
        model = ExtractionJob


class ExtractionTaskTable(AnalysisTaskTable):
    class Meta(AnalysisTaskTable.Meta):
        model = ExtractionTask
        empty_text = "No extraction tasks to show"
        fields = ("id", "status", "message", "ended_at")


class ExtractionInstanceTable(tables.Table):
    class Meta:
        model = ExtractionInstance
        empty_text = "No extraction instances to show"
        fields = ("id", "is_processed")
        attrs = {"class": "table table-bordered table-hover"}


class ExtractionResultsTable(tables.Table):
    id = tables.LinkColumn("extraction_instance_detail", args=[tables.A("id")])

    class Meta:
        model = ExtractionInstance
        empty_text = "No extraction results to show"
        fields = ("id",)
        attrs = {"class": "table table-bordered table-hover"}
