from logging import getLogger
from typing import Any, Type, cast

from adit_radis_shared.common.mixins import (
    PageSizeSelectMixin,
    RelatedFilterMixin,
    RelatedPaginationMixin,
)
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Count, QuerySet
from django.http import HttpResponse
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView
from django_tables2 import SingleTableView

from radis.subscriptions.filters import SubscriptionFilter
from radis.subscriptions.tables import SubscriptionTable

from .forms import SubscriptionForm
from .models import SubscribedItem, Subscription

logger = getLogger(__name__)


class SubscriptionListView(LoginRequiredMixin, SingleTableView):
    model = Subscription
    table_class = SubscriptionTable
    filterset_class = SubscriptionFilter
    paginate_by = 30
    request: AuthenticatedHttpRequest

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context

    def get_queryset(self) -> QuerySet[Subscription]:
        return Subscription.objects.filter(owner=self.request.user).annotate(
            num_reports=Count("items")
        )


class SubscriptionCreateView(LoginRequiredMixin, CreateView):  # TODO: Add PermissionRequiredMixin
    template_name = "subscriptions/subscription_create.html"
    form_class = SubscriptionForm
    success_url = reverse_lazy("subscription_list")
    request: AuthenticatedHttpRequest

    def form_valid(self, form) -> HttpResponse:
        user = self.request.user
        form.instance.owner = user

        group = user.active_group
        assert group, "User has no active group"
        form.instance.group = group

        try:
            return super().form_valid(form)
        except IntegrityError as e:
            if "unique_subscription_name_per_user" in str(e):
                form.add_error("name", "An subscription with this name already exists.")
                return self.form_invalid(form)
            raise e


class SubscriptionDetailView(LoginRequiredMixin, DetailView):
    model = Subscription
    template_name = "subscriptions/subscription_detail.html"

    def get_queryset(self):
        return super().get_queryset().filter(owner=self.request.user)


class SubscriptionUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "subscriptions/subscription_update.html"
    form_class = SubscriptionForm
    success_url = reverse_lazy("subscription_list")
    model = Subscription

    def get_queryset(self):
        return super().get_queryset().filter(owner=self.request.user)

    def form_valid(self, form) -> HttpResponse:
        try:
            return super().form_valid(form)
        except IntegrityError as e:
            if "unique_subscription_name_per_user" in str(e):
                form.add_error("name", "A subscription with this name already exists.")
                return self.form_invalid(form)
            raise e


class SubscriptionDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
    model = Subscription
    success_url = reverse_lazy("subscription_list")
    success_message = "Subscription successfully deleted"

    def get_queryset(self) -> QuerySet[Subscription]:
        return super().get_queryset().filter(owner=self.request.user)


class SubscriptionInboxView(
    LoginRequiredMixin,
    RelatedPaginationMixin,
    RelatedFilterMixin,
    PageSizeSelectMixin,
    DetailView,
):
    model = Subscription
    template_name = "subscriptions/subscription_inbox.html"
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Subscription]:
        assert self.model
        model = cast(Type[Subscription], self.model)
        if self.request.user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get_related_queryset(self) -> QuerySet[SubscribedItem]:
        subscription = cast(Subscription, self.get_object())
        return SubscribedItem.objects.filter(subscription_id=subscription.id).prefetch_related(
            "report"
        )

    def get_filter_queryset(self) -> QuerySet[SubscribedItem]:
        return self.get_related_queryset()
