from typing import Any

from adit_radis_shared.common.mixins import HtmxOnlyMixin, PageSizeSelectMixin
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.generic import DetailView, UpdateView, View
from django_filters.views import FilterView
from django_htmx.http import trigger_client_event

from radis.notes.filters import NoteFilter
from radis.notes.forms import NoteEditForm
from radis.notes.models import Note
from radis.reports.models import Report


class NoteListView(LoginRequiredMixin, PageSizeSelectMixin, FilterView):
    template_name = "notes/note_list.html"
    context_object_name = "notes"
    filterset_class = NoteFilter
    paginate_by = 10
    page_sizes = [10, 25, 50]
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Note]:
        return Note.objects.filter(
            owner=self.request.user, report__withdrawn_at__isnull=True
        ).order_by("-pk")


class NoteDetailView(LoginRequiredMixin, HtmxOnlyMixin, DetailView):
    template_name = "notes/_note_detail.html"
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Note]:
        return Note.objects.filter(owner=self.request.user, report__withdrawn_at__isnull=True)


class NoteEditView(LoginRequiredMixin, HtmxOnlyMixin, UpdateView):
    """A combined note create/update view to be presented in a dialog.

    Also deletes the note if the text is empty.
    """

    model = Note
    form_class = NoteEditForm
    template_name = "notes/_note_edit.html"
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        # Creating or editing a note must not resurrect access to a withdrawn
        # report; the guard covers GET (dialog) and POST (save) alike.
        get_object_or_404(Report.objects.live(), pk=self.kwargs["report_id"])
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset: QuerySet[Note] | None = None) -> Note | None:
        if queryset is None:
            queryset = self.get_queryset()

        report_id: int = self.kwargs["report_id"]
        return Note.objects.filter(report_id=report_id, owner=self.request.user).first()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["report_id"] = self.kwargs["report_id"]
        return context

    def form_valid(self, form: NoteEditForm) -> HttpResponse:
        report_id: int = self.kwargs["report_id"]

        note: Note = form.instance
        note.owner = self.request.user
        note.report_id = report_id

        text: str = note.text
        if note.pk is not None and len(text.strip()) == 0:
            note.delete()
            response = HttpResponse(status=204)
            response = trigger_client_event(response, f"noteChanged_{report_id}")
            response = trigger_client_event(response, "noteDeleted")
            return response

        elif len(text.strip()) > 0:
            form.save()

        response = HttpResponse(status=204)
        response = trigger_client_event(response, f"noteChanged_{report_id}")
        return response


class NoteAvailableBadgeView(LoginRequiredMixin, HtmxOnlyMixin, View):
    def get(self, request: AuthenticatedHttpRequest, report_id: int) -> HttpResponse:
        report = get_object_or_404(Report.objects.live(), id=report_id)

        return render(
            request,
            "notes/_note_available_badge.html",
            {"report": report},
        )
