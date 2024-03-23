from typing import Any

from django.contrib.auth.mixins import (
    LoginRequiredMixin,
    UserPassesTestMixin,
)
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.urls import re_path
from django.views.generic import View
from django.views.generic.base import TemplateView
from revproxy.views import ProxyView

from .types import AuthenticatedHttpRequest, HtmxHttpRequest


class HtmxTemplateView(TemplateView):
    def get(self, request: HtmxHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.htmx:
            raise SuspiciousOperation
        return super().get(request, *args, **kwargs)


class BaseUpdatePreferencesView(LoginRequiredMixin, View):
    """Allows the client to update the user preferences.

    We use this to retain some form state between browser refreshes.
    The implementations of this view is called by some AJAX requests when specific
    form fields are changed.
    """

    allowed_keys: list[str]

    def post(self, request: AuthenticatedHttpRequest) -> HttpResponse:
        for key in request.POST.keys():
            if key not in self.allowed_keys:
                raise SuspiciousOperation(f'Invalid preference "{key}" to update.')

        preferences = request.user.preferences

        for key, value in request.POST.items():
            if value == "true":
                value = True
            elif value == "false":
                value = False

            preferences[key] = value

        request.user.save()

        return HttpResponse()


class AdminProxyView(LoginRequiredMixin, UserPassesTestMixin, ProxyView):
    """A reverse proxy view to hide other services behind that only an admin can access.

    By using a reverse proxy we can use the Django authentication
    to check for an logged in admin user.
    Code from https://stackoverflow.com/a/61997024/166229
    """

    request: AuthenticatedHttpRequest

    def test_func(self):
        return self.request.user.is_staff

    @classmethod
    def as_url(cls):
        return re_path(rf"^{cls.url_prefix}/(?P<path>.*)$", cls.as_view())  # type: ignore
