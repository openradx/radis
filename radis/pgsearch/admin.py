import logging

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Func, IntegerField, Sum
from django.db.models.fields.json import KeyTransform
from django.db.models.query import QuerySet
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.http.request import HttpRequest
from django.urls import path, reverse
from procrastinate.contrib.django.models import ProcrastinateJob

from .models import EmbeddingBackfillRun, ReportSearchIndex
from .tasks import (
    ActiveBackfillError,
    cancel_backfill_embeddings,
    create_backfill_run,
    enqueue_embed_reports,
)

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
        doing about it right now — subjob counts per status, plus how many
        reports the queued and in-flight subjobs cover. The report totals
        are summed DB-side from each job's args->'report_ids'
        (jsonb_array_length); the id arrays never leave Postgres, which
        matters when a large backfill holds millions of ids in `todo` jobs."""
        pending = ReportSearchIndex.objects.filter(embedding__isnull=True).count()
        report_count = Func(
            KeyTransform("report_ids", "args"),
            function="jsonb_array_length",
            output_field=IntegerField(),
        )
        queue_rows = {
            row["status"]: row
            for row in ProcrastinateJob.objects.filter(queue_name="embeddings")
            .values("status")
            .annotate(jobs=Count("id"), reports=Sum(report_count))
        }
        # Counted separately because the cancel-backfill button only cancels
        # backfill-priority jobs — gating it on the overall todo count would
        # offer a cancel that then reports "nothing to cancel" whenever the
        # queue holds only live write-path jobs.
        todo_backfill = ProcrastinateJob.objects.filter(
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).count()
        todo_row = queue_rows.get("todo", {})
        doing_row = queue_rows.get("doing", {})
        return {
            "pending_reports": pending,
            "todo": todo_row.get("jobs", 0),
            # Sum() returns NULL when no job has report_ids — coalesce here.
            "todo_reports": todo_row.get("reports") or 0,
            "todo_backfill": todo_backfill,
            "doing": doing_row.get("jobs", 0),
            "doing_reports": doing_row.get("reports") or 0,
            "failed": queue_rows.get("failed", {}).get("jobs", 0),
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

        try:
            run = create_backfill_run(len(report_ids), triggered_by=request.user.get_username())
        except ActiveBackfillError as exc:
            self.message_user(request, str(exc), level=messages.WARNING)
            return

        subjob_count = enqueue_embed_reports(
            report_ids, priority=settings.EMBEDDING_BACKFILL_PRIORITY, run_id=run.pk
        )

        self.message_user(
            request,
            f"Enqueued {len(report_ids)} report(s) across {subjob_count} subjob(s) "
            f"for embedding (run {run.pk}).",
            level=messages.SUCCESS,
        )
        logger.info(
            "admin.enqueue_pending_embeddings: user=%s enqueued %d report(s) across "
            "%d subjob(s) for run %d",
            request.user.get_username(),
            len(report_ids),
            subjob_count,
            run.pk,
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


@admin.register(EmbeddingBackfillRun)
class EmbeddingBackfillRunAdmin(admin.ModelAdmin):
    """Read-only backfill history ("what did last night's backfill do?").
    Runs are created by embed_pending / the admin enqueue action and
    mutated only by the task counter and cancel — never by hand."""

    list_display = (
        "id",
        "started_at",
        "finished_at",
        "cancelled_at",
        "processed_reports",
        "total_reports",
        "triggered_by",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
