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
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView
from django_tables2 import SingleTableView

from radis.subscriptions.filters import SubscribedItemFilter, SubscriptionFilter
from radis.subscriptions.tables import SubscriptionTable

from .forms import (
    FilterQuestionFormSet,
    OutputFieldFormSet,
    SubscriptionForm,
)
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
        return (
            Subscription.objects.filter(owner=self.request.user)
            .annotate(num_reports=Count("items"))
            .order_by("-created_at")
        )


class SubscriptionDetailView(LoginRequiredMixin, DetailView):
    model = Subscription
    template_name = "subscriptions/subscription_detail.html"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(owner=self.request.user)
            .prefetch_related("filter_questions", "output_fields")
        )


class SubscriptionCreateView(LoginRequiredMixin, CreateView):  # TODO: Add PermissionRequiredMixin
    template_name = "subscriptions/subscription_create.html"
    form_class = SubscriptionForm
    success_url = reverse_lazy("subscription_list")
    request: AuthenticatedHttpRequest

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx["filter_formset"] = FilterQuestionFormSet(self.request.POST)
            ctx["output_formset"] = OutputFieldFormSet(self.request.POST)
        else:
            ctx["filter_formset"] = FilterQuestionFormSet()
            ctx["output_formset"] = OutputFieldFormSet()
        return ctx

    def form_valid(self, form) -> HttpResponse:
        ctx = self.get_context_data()
        filter_formset: BaseInlineFormSet = ctx["filter_formset"]
        output_formset: BaseInlineFormSet = ctx["output_formset"]
        if filter_formset.is_valid() and output_formset.is_valid():
            user = self.request.user
            form.instance.owner = user
            active_group = user.active_group
            form.instance.group = active_group

            try:
                self.object: Subscription = form.save()
            except IntegrityError as e:
                if "unique_subscription_name_per_user" in str(e):
                    form.add_error("name", "A subscription with this name already exists.")
                    return self.form_invalid(form)
                raise e

            filter_formset.instance = self.object
            filter_formset.save()

            output_formset.instance = self.object
            output_formset.save()
            return HttpResponseRedirect(self.get_success_url())
        else:
            return self.form_invalid(form)


class SubscriptionUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "subscriptions/subscription_update.html"
    form_class = SubscriptionForm
    model = Subscription
    request: AuthenticatedHttpRequest

    def get_success_url(self):
        return reverse("subscription_detail", kwargs={"pk": self.object.pk})

    def get_queryset(self) -> QuerySet[Subscription]:
        return (
            super()
            .get_queryset()
            .filter(owner=self.request.user)
            .prefetch_related("filter_questions", "output_fields")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx["filter_formset"] = FilterQuestionFormSet(self.request.POST, instance=self.object)
            ctx["output_formset"] = OutputFieldFormSet(self.request.POST, instance=self.object)
        else:
            ctx["filter_formset"] = FilterQuestionFormSet(instance=self.object)
            ctx["output_formset"] = OutputFieldFormSet(instance=self.object)
        ctx["filter_formset"].extra = 0  # no additional empty form when editing
        ctx["output_formset"].extra = 0
        return ctx

    def form_valid(self, form) -> HttpResponse:
        ctx = self.get_context_data()
        filter_formset = ctx["filter_formset"]
        output_formset = ctx["output_formset"]
        if filter_formset.is_valid() and output_formset.is_valid():
            try:
                self.object = form.save()
            except IntegrityError as e:
                if "unique_subscription_name_per_user" in str(e):
                    form.add_error("name", "A subscription with this name already exists.")
                    return self.form_invalid(form)
                raise e

            filter_formset.instance = self.object
            filter_formset.save()

            output_formset.instance = self.object
            output_formset.save()

            return super().form_valid(form)
        else:
            return self.form_invalid(form)


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
    filterset_class = SubscribedItemFilter
    paginate_by = 10
    page_sizes = [10, 25, 50]
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Subscription]:
        assert self.model
        model = cast(Type[Subscription], self.model)
        if self.request.user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get_ordering(self) -> str:
        """Get the ordering from query parameters, defaulting to -created_at."""
        sort_by = self.request.GET.get("sort_by", "created_at")
        order = self.request.GET.get("order", "desc")

        # Define allowed sort fields to prevent injection
        allowed_fields = {
            "created_at": "created_at",
            "study_date": "report__study_datetime",
        }

        # Validate sort_by parameter
        if sort_by not in allowed_fields:
            sort_by = "created_at"

        # Validate order parameter
        if order not in ["asc", "desc"]:
            order = "desc"

        field = allowed_fields[sort_by]
        return field if order == "asc" else f"-{field}"

    def get_related_queryset(self) -> QuerySet[SubscribedItem]:
        subscription = cast(Subscription, self.get_object())
        ordering = self.get_ordering()
        return (
            SubscribedItem.objects.filter(subscription_id=subscription.pk)
            .select_related("subscription")
            .prefetch_related(
                "report",
                "subscription__output_fields",
            )
            .order_by(ordering)
        )

    def get_filter_queryset(self) -> QuerySet[SubscribedItem]:
        return self.get_related_queryset()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        # Get validated sort parameters
        sort_by = self.request.GET.get("sort_by", "created_at")
        order = self.request.GET.get("order", "desc")

        # Define allowed sort fields (same as in get_ordering)
        allowed_fields = {
            "created_at": "created_at",
            "study_date": "report__study_datetime",
        }

        # Validate and default if invalid
        if sort_by not in allowed_fields:
            sort_by = "created_at"
        if order not in ["asc", "desc"]:
            order = "desc"

        # Add validated sort parameters to context for template rendering
        context["current_sort_by"] = sort_by
        context["current_order"] = order

        return context
