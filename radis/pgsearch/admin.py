from django.contrib import admin, messages
from django.db.models import Count
from django.db.models.query import QuerySet
from django.http.request import HttpRequest
from procrastinate.contrib.django.models import ProcrastinateJob

from .models import ReportSearchIndex
from .tasks import enqueue_embed_reports

EMBEDDINGS_QUEUE = "embeddings"


@admin.register(ReportSearchIndex)
class ReportSearchIndexAdmin(admin.ModelAdmin):
    list_display = ("id", "report_id", "has_embedding")
    list_filter = (("embedding", admin.EmptyFieldListFilter),)
    search_fields = ("report__document_id",)
    actions = ("enqueue_pending_embeddings",)
    change_list_template = "admin/pgsearch/reportsearchindex/change_list.html"

    @admin.display(boolean=True, description="Embedded")
    def has_embedding(self, obj: ReportSearchIndex) -> bool:
        return obj.embedding is not None

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["embedding_pipeline_stats"] = self._embedding_pipeline_stats()
        return super().changelist_view(request, extra_context=extra_context)

    @staticmethod
    def _embedding_pipeline_stats() -> dict[str, int]:
        """Snapshot of the embedding pipeline for the admin badge: how many
        reports are still missing an embedding, and what Procrastinate is
        doing about it right now."""
        pending = ReportSearchIndex.objects.filter(embedding__isnull=True).count()
        queue_counts = dict(
            ProcrastinateJob.objects.filter(queue_name=EMBEDDINGS_QUEUE)
            .values_list("status")
            .annotate(n=Count("id"))
        )
        return {
            "pending_reports": pending,
            "todo": queue_counts.get("todo", 0),
            "doing": queue_counts.get("doing", 0),
            "failed": queue_counts.get("failed", 0),
        }

    @admin.action(description="Enqueue embedding for selected rows (NULL only)")
    def enqueue_pending_embeddings(
        self, request: HttpRequest, queryset: QuerySet[ReportSearchIndex]
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

        subjob_count = enqueue_embed_reports(report_ids)

        self.message_user(
            request,
            f"Enqueued {len(report_ids)} report(s) across "
            f"{subjob_count} subjob(s) for embedding.",
            level=messages.SUCCESS,
        )
