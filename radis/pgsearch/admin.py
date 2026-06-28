from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count
from django.db.models.query import QuerySet
from django.http.request import HttpRequest
from procrastinate.contrib.django.models import ProcrastinateJob

from .models import ReportSearchIndex
from .tasks import enqueue_embed_reports


@admin.register(ReportSearchIndex)
class ReportSearchIndexAdmin(admin.ModelAdmin):
    list_display = ("id", "report_id", "has_embedding")
    list_filter = (("embedding", admin.EmptyFieldListFilter),)
    search_fields = ("report__document_id",)
    actions = ("enqueue_pending_embeddings", "clear_embeddings_for_remodel")
    change_list_template = "admin/pgsearch/reportsearchindex/change_list.html"

    def has_delete_permission(self, request, obj=None):
        # RSI rows are managed by the post_save signal on Report — deleting
        # one orphans the report from search until someone saves the report
        # again. Block delete (this also hides the "delete selected" action).
        return False

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
            ProcrastinateJob.objects.filter(queue_name="embeddings")
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

        subjob_count = enqueue_embed_reports(
            report_ids, priority=settings.EMBEDDING_BACKFILL_PRIORITY
        )

        self.message_user(
            request,
            f"Enqueued {len(report_ids)} report(s) across "
            f"{subjob_count} subjob(s) for embedding.",
            level=messages.SUCCESS,
        )

    @admin.action(description="Clear embeddings (NULL them) — for same-dim model swap")
    def clear_embeddings_for_remodel(
        self, request: HttpRequest, queryset: QuerySet[ReportSearchIndex]
    ) -> None:
        # Same-dim model swap procedure: NULL the existing embeddings so
        # the new model writes fresh ones via `embed_pending`. Uses
        # queryset.update so post_save signals don't fire (we don't want
        # auto-re-embedding here — that'd hit the embedding service
        # immediately, possibly with the OLD model still configured).
        # The operator drives the backfill explicitly afterward.
        cleared = queryset.filter(embedding__isnull=False).update(embedding=None)
        if not cleared:
            self.message_user(
                request,
                "No selected rows had an embedding to clear.",
                level=messages.WARNING,
            )
            return
        self.message_user(
            request,
            f"Cleared embeddings on {cleared} row(s). Run "
            f"`./manage.py embed_pending` (or the 'Enqueue embedding' "
            f"action) to backfill against the new model.",
            level=messages.SUCCESS,
        )
