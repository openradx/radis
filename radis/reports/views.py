from typing import Any, cast

from adit_radis_shared.common.mixins import HtmxOnlyMixin
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import render
from django.views.generic import View
from django.views.generic.detail import DetailView, SingleObjectMixin

from radis.core.utils.chat_client import ChatClient
from radis.reports.forms import PromptForm

from .models import Report


class ReportDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Report
    template_name = "reports/report_detail.html"
    context_object_name = "report"
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_queryset(self) -> QuerySet[Report]:
        active_group = self.request.user.active_group
        assert active_group
        return super().get_queryset().filter(groups=active_group)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["prompt_form"] = PromptForm()
        context["messages"] = []
        return context


class ReportBodyView(ReportDetailView):
    template_name = "reports/report_body.html"


# TODO: Make this an async view
class ReportChatView(
    LoginRequiredMixin, HtmxOnlyMixin, SingleObjectMixin, UserPassesTestMixin, View
):
    model = Report
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_queryset(self) -> QuerySet[Report]:
        active_group = self.request.user.active_group
        assert active_group
        return super().get_queryset().filter(groups=active_group)

    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        report = cast(Report, self.get_object())
        form = PromptForm(request.POST)

        context: dict[str, Any] = {
            "messages": [],
            "report": report,
            "prompt_form": form,
        }

        if form.is_valid():
            chat_client = ChatClient()
            if request.POST.get("yes_no_answer"):
                answer = chat_client.ask_yes_no_question(
                    report.body, report.language.code, form.cleaned_data["prompt"]
                )
                answer = "Yes" if answer == "yes" else "No"
            elif request.POST.get("full_answer"):
                answer = chat_client.ask_question(
                    report.body, report.language.code, form.cleaned_data["prompt"]
                )
            else:
                raise ValueError("Invalid form")

            context["messages"] = [
                {"role": "User", "content": form.cleaned_data["prompt"]},
                {"role": "Assistant", "content": answer},
            ]
            context["prompt_form"] = PromptForm()

        return render(request, "reports/_report_chat.html", context)
