from django.conf import settings
from django.contrib import admin, messages
from django.db.models.query import QuerySet
from django.http.request import HttpRequest

from .models import ReportSearchVector
from .tasks import embed_reports_task


@admin.register(ReportSearchVector)
class ReportSearchVectorAdmin(admin.ModelAdmin):
    list_display = ("id", "report_id", "has_embedding")
    list_filter = ("embedding",)
    search_fields = ("report__document_id",)
    actions = ("enqueue_pending_embeddings",)

    @admin.display(boolean=True, description="Embedded")
    def has_embedding(self, obj: ReportSearchVector) -> bool:
        return obj.embedding is not None

    @admin.action(description="Enqueue embedding for selected rows (NULL only)")
    def enqueue_pending_embeddings(
        self, request: HttpRequest, queryset: QuerySet[ReportSearchVector]
    ) -> None:
        report_ids = list(
            queryset.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if not report_ids:
            self.message_user(
                request,
                "No selected rows are missing an embedding.",
                level=messages.WARNING,
            )
            return

        batch_size = settings.EMBEDDING_BATCH_SIZE
        for i in range(0, len(report_ids), batch_size):
            chunk = report_ids[i : i + batch_size]
            embed_reports_task.defer(report_ids=list(chunk))

        self.message_user(
            request,
            f"Enqueued {len(report_ids)} report(s) for embedding.",
            level=messages.SUCCESS,
        )
