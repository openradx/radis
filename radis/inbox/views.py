from logging import getLogger
from typing import Any

from django.contrib.auth.mixins import (LoginRequiredMixin,
                                        PermissionRequiredMixin)
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import QuerySet
from django.http import HttpResponse
from django.urls import reverse_lazy
from django.views.generic import (CreateView, DeleteView, DetailView, ListView,
                                  UpdateView)
from django_htmx.http import trigger_client_event

from .forms import SearchForm
from .models import Inbox

logger = getLogger(__name__)


class InboxCreateView(CreateView, LoginRequiredMixin):  # TODO: Add PermissionRequiredMixin
    template_name = "inbox/_inbox_create.html"
    form_class = SearchForm

    def form_valid(self, form) -> HttpResponse:
        user = self.request.user
        group = user.active_group
        assert group

        form.instance.owner = user
        form.instance.owner_id = user.id
        form.instance.group = group

        inbox: Inbox = form.save(commit=False)

        if inbox.age_from is not None and inbox.age_till is not None:
            if inbox.age_from > inbox.age_till:
                form.add_error(
                    "age_from",
                    "The minimum age must be less than or equal to the maximum age",
                )
                return self.form_invalid(form)

        try:
            inbox.save()
        except Exception as e:
            if "unique_inbox_name_per_user" in str(e):
                form.add_error("name", "An inbox with this name already exists.")
                return self.form_invalid(form)
            raise e

        response = HttpResponse(status=204)
        return trigger_client_event(response, "inboxListChanged")


class InboxListView(ListView):
    model = Inbox

    def get_template_names(self) -> list[str]:
        if self.request.htmx:
            return ["inbox/_inbox_list.html"]
        return ["inbox/inbox_list.html"]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context

    def get_queryset(self) -> QuerySet[Inbox]:
        return Inbox.objects.filter(owner=self.request.user)


class InboxDetailView(LoginRequiredMixin, DetailView):
    model = Inbox
    template_name = "inbox/inbox_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context


class InboxDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
    model = Inbox
    success_url = reverse_lazy("inbox_list")
    success_message = "Inbox successfully deleted"

    def get_queryset(self) -> QuerySet[Inbox]:
        return super().get_queryset().filter(owner=self.request.user)
