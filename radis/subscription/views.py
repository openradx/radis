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
from django.db.models import QuerySet
from django.http import HttpResponse
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView
from django_htmx.http import trigger_client_event

from .forms import SearchForm
from .models import SubscribedItem, Subscription

logger = getLogger(__name__)


class SubscriptionCreateView(CreateView, LoginRequiredMixin):  # TODO: Add PermissionRequiredMixin
    template_name = "subscription/_subscription_create.html"
    form_class = SearchForm
    request: AuthenticatedHttpRequest

    def form_valid(self, form) -> HttpResponse:
        user = self.request.user
        group = user.active_group
        assert group

        form.instance.owner = user
        form.instance.owner_id = user.id
        form.instance.group = group

        subscription: Subscription = form.save(commit=False)

        if subscription.age_from is not None and subscription.age_till is not None:
            if subscription.age_from > subscription.age_till:
                form.add_error(
                    "age_from",
                    "The minimum age must be less than or equal to the maximum age",
                )
                return self.form_invalid(form)

        try:
            subscription.save()
        except Exception as e:
            if "unique_subscription_name_per_user" in str(e):
                form.add_error("name", "An subscription with this name already exists.")
                return self.form_invalid(form)
            raise e

        response = HttpResponse(status=204)
        return trigger_client_event(response, "subscriptionListChanged")


class SubscriptionListView(ListView):
    model = Subscription
    request: AuthenticatedHttpRequest

    def get_template_names(self) -> list[str]:
        if self.request.htmx:
            return ["subscription/_subscription_list.html"]
        return ["subscription/subscription_list.html"]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context

    def get_queryset(self) -> QuerySet[Subscription]:
        return Subscription.objects.filter(owner=self.request.user)


class SubscriptionDetailView(LoginRequiredMixin, DetailView):
    model = Subscription
    template_name = "subscription/subscription_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context


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
    template_name = "subscription/subscription_inbox.html"
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
