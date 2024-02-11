from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import SuspiciousOperation
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from radis.core.types import AuthenticatedHttpRequest
from radis.reports.models import Report

from .models import VespaReFeed
from .tasks import re_feed_reports


class VespaAdmin(LoginRequiredMixin, UserPassesTestMixin, View):
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.is_staff

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        current_re_feed = VespaReFeed.objects.filter(
            Q(status=VespaReFeed.PENDING) | Q(status=VespaReFeed.IN_PROGRESS)
        ).first()

        previous_re_feed = VespaReFeed.objects.order_by("-created").first()

        if current_re_feed:
            assert previous_re_feed
            assert current_re_feed.id == previous_re_feed.id

        report_count = Report.objects.all().count()

        context: dict[str, Any] = {
            "report_count": report_count,
            "current_re_feed": current_re_feed,
            "previous_re_feed": previous_re_feed,
        }

        return render(request, "vespa/vespa_admin.html", context)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        running_re_feed = VespaReFeed.objects.filter(
            Q(status=VespaReFeed.PENDING) | Q(status=VespaReFeed.IN_PROGRESS)
        ).first()

        if running_re_feed:
            raise SuspiciousOperation("A re-feed is already running.")

        new_re_feed = VespaReFeed.objects.create()
        re_feed_reports.delay(new_re_feed.id)

        messages.add_message(request, messages.SUCCESS, "Re-feed started.")

        return redirect("vespa_admin")
