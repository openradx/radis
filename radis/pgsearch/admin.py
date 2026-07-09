import logging

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.db.models.query import QuerySet
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.http.request import HttpRequest
from django.urls import path, reverse
from procrastinate.contrib.django.models import ProcrastinateJob

from .models import ReportSearchIndex
from .tasks import cancel_backfill_embeddings, enqueue_embed_reports

logger = logging.getLogger(__name__)


@admin.register(ReportSearchIndex)
class ReportSearchIndexAdmin(admin.ModelAdmin):
    list_display = ("id", "report_id", "has_embedding")
    list_filter = (("embedding", admin.EmptyFieldListFilter),)
    search_fields = ("report__document_id",)
    actions = ("enqueue_pending_embeddings", "clear_embeddings")
    change_list_template = "admin/pgsearch/reportsearchindex/change_list.html"
    # Unconfigured, `report` (OneToOneField) renders as a <select> populated
    # with every Report row — millions of rows in production — on every change-form
    # load. raw_id_fields swaps that for a text input + lookup popup so
    # opening a single row doesn't enumerate the whole table.
    raw_id_fields = ("report",)

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

    def get_urls(self):
        return [
            path(
                "cancel-backfill/",
                self.admin_site.admin_view(self.cancel_backfill_view),
                name="pgsearch_reportsearchindex_cancel_backfill",
            ),
            *super().get_urls(),
        ]

    def cancel_backfill_view(self, request: HttpRequest) -> HttpResponse:
        """Queue-scoped counterpart to `embed_cancel`: cancel every queued
        backfill subjob. A custom view (not an admin action) because admin
        actions are row-scoped and cancelling the queue has nothing to do
        with selected ReportSearchIndex rows."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        cancelled = cancel_backfill_embeddings()
        if cancelled:
            self.message_user(
                request,
                f"Cancelled {cancelled} queued backfill subjob(s). Running "
                f"subjobs finish their current chunk. Re-running embed_pending "
                f"enqueues the still-unembedded reports as fresh subjobs "
                f"(chunked at the current subjob size) — cancelled jobs are "
                f"never revived.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                "No queued backfill subjobs to cancel.",
                level=messages.WARNING,
            )
        logger.info(
            "admin.cancel_backfill: user=%s cancelled %d subjob(s)",
            request.user.get_username(),
            cancelled,
        )
        return HttpResponseRedirect(reverse("admin:pgsearch_reportsearchindex_changelist"))

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
        # Counted separately because the cancel-backfill button only cancels
        # backfill-priority jobs — gating it on the overall todo count would
        # offer a cancel that then reports "nothing to cancel" whenever the
        # queue holds only live write-path jobs.
        todo_backfill = ProcrastinateJob.objects.filter(
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).count()
        return {
            "pending_reports": pending,
            "todo": queue_counts.get("todo", 0),
            "todo_backfill": todo_backfill,
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
            f"Enqueued {len(report_ids)} report(s) across {subjob_count} subjob(s) for embedding.",
            level=messages.SUCCESS,
        )
        logger.info(
            "admin.enqueue_pending_embeddings: user=%s enqueued %d report(s) across %d subjob(s)",
            request.user.get_username(),
            len(report_ids),
            subjob_count,
        )

    @admin.action(description="Clear embeddings (NULL them)")
    def clear_embeddings(self, request: HttpRequest, queryset: QuerySet[ReportSearchIndex]) -> None:
        # NULL the embeddings so `embed_pending` writes fresh ones (e.g.
        # after a same-dim model swap). Uses queryset.update so post_save
        # signals don't fire (we don't want auto-re-embedding here — that'd
        # hit the embedding service immediately, possibly with the OLD model
        # still configured). The operator drives the backfill explicitly.
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
        logger.info(
            "admin.clear_embeddings: user=%s cleared %d embedding(s)",
            request.user.get_username(),
            cleared,
        )
