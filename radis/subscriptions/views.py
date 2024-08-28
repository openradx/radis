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

from radis.subscriptions.filters import SubscriptionFilter
from radis.subscriptions.tables import SubscriptionTable

from .forms import SubscriptionAndQuestionForm, SubscriptionForm
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
    form_class = SubscriptionAndQuestionForm
    success_url = reverse_lazy("subscription_list")
    request: AuthenticatedHttpRequest

    def form_valid(self, form) -> HttpResponse:
        user = self.request.user
        subscription_form = cast(SubscriptionForm, form["subscription"])
        question_formset = cast(BaseInlineFormSet, form["questions"])

        subscription_form.instance.owner = self.request.user

        group = user.active_group
        assert group, "User has no active group"
        subscription_form.instance.group = group

        try:
            self.object = subscription_form.save()
        except IntegrityError as e:
            if "unique_subscription_name_per_user" in str(e):
                form.add_error("name", "An subscription with this name already exists.")
                return self.form_invalid(form)
            raise e

        question_formset.instance = self.object
        question_formset.save()

        return HttpResponseRedirect(self.get_success_url())


class SubscriptionDetailView(LoginRequiredMixin, DetailView):
    model = Subscription
    template_name = "subscriptions/subscription_detail.html"

    def get_queryset(self):
        return super().get_queryset().filter(owner=self.request.user).prefetch_related("questions")


class SubscriptionUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "subscriptions/subscription_update.html"
    form_class = SubscriptionAndQuestionForm
    model = Subscription
    request: AuthenticatedHttpRequest

    def get_success_url(self):
        return reverse("subscription_detail", kwargs={"pk": self.object.pk})

    def get_form_kwargs(self):
        kwargs = super(SubscriptionUpdateView, self).get_form_kwargs()
        kwargs["instance"] = {
            "subscription": self.object,
            "questions": self.object,
        }
        kwargs["queryset"] = {
            "questions": self.object.questions.all(),
        }
        return kwargs

    def form_valid(self, form) -> HttpResponse:
        user = self.request.user
        subscription_form = cast(SubscriptionForm, form["subscription"])
        question_formset = cast(BaseInlineFormSet, form["questions"])

        subscription_form.instance.owner = self.request.user

        group = user.active_group
        assert group, "User has no active group"
        subscription_form.instance.group = group

        try:
            self.object = subscription_form.save()
        except IntegrityError as e:
            if "unique_subscription_name_per_user" in str(e):
                form.add_error("name", "An subscription with this name already exists.")
                return self.form_invalid(form)
            raise e

        question_formset.instance = self.object
        question_formset.save()

        return HttpResponseRedirect(self.get_success_url())


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
        return SubscribedItem.objects.filter(subscription_id=subscription.pk).prefetch_related(
            "report"
        )

    def get_filter_queryset(self) -> QuerySet[SubscribedItem]:
        return self.get_related_queryset()
