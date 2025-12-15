import csv
from collections.abc import Generator
from logging import getLogger
from typing import Any, Type, cast

from adit_radis_shared.accounts.models import User
from adit_radis_shared.common.mixins import (
    PageSizeSelectMixin,
    RelatedFilterMixin,
    RelatedPaginationMixin,
)
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Count, F, Q, QuerySet
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
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
from .utils.csv_export import iter_subscribed_item_rows

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
            .annotate(
                num_new_reports=Count(
                    "items",
                    filter=Q(items__created_at__gt=F("last_viewed_at"))
                    | Q(last_viewed_at__isnull=True),
                )
            )
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

            # Auto-generate query if needed
            if not form.cleaned_data.get("query", "").strip():
                # Collect output fields from formset
                temp_fields = []
                for field_form in output_formset:
                    if (
                        field_form.cleaned_data
                        and not field_form.cleaned_data.get("DELETE", False)
                        and field_form.cleaned_data.get("name")
                    ):
                        from radis.extractions.models import OutputField

                        temp_fields.append(
                            OutputField(
                                name=field_form.cleaned_data["name"],
                                description=field_form.cleaned_data["description"],
                                output_type=field_form.cleaned_data["output_type"],
                            )
                        )

                if temp_fields:
                    from radis.extractions.utils.query_generator import QueryGenerator

                    generator = QueryGenerator()
                    generated_query, metadata = generator.generate_from_fields(temp_fields)

                    # Check if generation failed
                    if generated_query is None or not metadata.get("success"):
                        form.add_error(
                            "query",
                            "Unable to automatically generate a query from your extraction fields. "
                            "Please manually enter a search query.",
                        )
                        return self.form_invalid(form)

                    form.instance.query = generated_query
                    logger.info(
                        f"Auto-generated query for subscription: {generated_query} "
                        f"(method: {metadata.get('generation_method')})"
                    )

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
            # Auto-generate query if needed
            if not form.cleaned_data.get("query", "").strip():
                # Collect output fields from formset
                temp_fields = []
                for field_form in output_formset:
                    if (
                        field_form.cleaned_data
                        and not field_form.cleaned_data.get("DELETE", False)
                        and field_form.cleaned_data.get("name")
                    ):
                        from radis.extractions.models import OutputField

                        temp_fields.append(
                            OutputField(
                                name=field_form.cleaned_data["name"],
                                description=field_form.cleaned_data["description"],
                                output_type=field_form.cleaned_data["output_type"],
                            )
                        )

                if temp_fields:
                    from radis.extractions.utils.query_generator import QueryGenerator

                    generator = QueryGenerator()
                    generated_query, metadata = generator.generate_from_fields(temp_fields)

                    # Check if generation failed
                    if generated_query is None or not metadata.get("success"):
                        form.add_error(
                            "query",
                            "Unable to automatically generate a query from your extraction fields. "
                            "Please manually enter a search query.",
                        )
                        return self.form_invalid(form)

                    form.instance.query = generated_query
                    logger.info(
                        f"Auto-generated query for subscription update: {generated_query} "
                        f"(method: {metadata.get('generation_method')})"
                    )

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

    def get_queryset(self) -> QuerySet[Subscription]:
        assert self.model
        model = cast(Type[Subscription], self.model)
        user = cast(User, self.request.user)
        if user.is_staff:
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

        # Update last_viewed_at to mark all current reports as seen
        subscription = cast(Subscription, self.object)
        subscription.last_viewed_at = timezone.now()
        subscription.save(update_fields=["last_viewed_at"])

        return context


class _Echo:
    """Lightweight write-only buffer for csv.writer."""

    def write(self, value: str) -> str:
        return value


class SubscriptionInboxDownloadView(LoginRequiredMixin, RelatedFilterMixin, DetailView):
    """Stream subscription inbox items as a CSV download.

    Applies the same filters as SubscriptionInboxView to ensure users
    download exactly what they see (respecting filters but ignoring pagination).
    """

    model = Subscription
    filterset_class = SubscribedItemFilter
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Subscription]:
        """Return only subscriptions owned by the current user."""
        assert self.model
        model = cast(Type[Subscription], self.model)
        user = cast(User, self.request.user)
        if user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get_ordering(self) -> str:
        """Get the ordering from query parameters (same logic as SubscriptionInboxView)."""
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
        """Build queryset matching the inbox view (for filtering)."""
        subscription = cast(Subscription, self.get_object())
        ordering = self.get_ordering()
        return (
            SubscribedItem.objects.filter(subscription_id=subscription.pk)
            .exclude(extraction_results__isnull=True)  # Only items with results
            .exclude(extraction_results={})  # Only items with non-empty results
            .select_related("subscription")
            .prefetch_related(
                "report",
                "report__modalities",
                "subscription__output_fields",
            )
            .order_by(ordering)
        )

    def get_filter_queryset(self) -> QuerySet[SubscribedItem]:
        """Required by RelatedFilterMixin."""
        return self.get_related_queryset()

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> StreamingHttpResponse:
        """Stream the CSV file response."""
        subscription = cast(Subscription, self.get_object())

        # Manually instantiate the filterset to apply filters
        # (RelatedFilterMixin doesn't provide get_filtered_queryset())
        filterset_class = self.get_filterset_class()
        filterset_kwargs = self.get_filterset_kwargs(filterset_class)
        assert filterset_class is not None
        filterset = filterset_class(**filterset_kwargs)

        # Get the filtered queryset from filterset.qs
        filtered_items = filterset.qs

        filename = self._build_filename(subscription)

        response = StreamingHttpResponse(
            self._stream_rows(subscription, filtered_items),
            content_type="text/csv",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _stream_rows(
        self, subscription: Subscription, items: QuerySet[SubscribedItem]
    ) -> Generator[str, None, None]:
        """Yield serialized CSV rows for the response."""
        pseudo_buffer = _Echo()
        writer = csv.writer(pseudo_buffer)
        yield "\ufeff"  # UTF-8 BOM for Excel compatibility
        for row in iter_subscribed_item_rows(subscription, items):
            yield writer.writerow(row)

    def _build_filename(self, subscription: Subscription) -> str:
        """Generate a descriptive CSV filename for the subscription."""
        slug = slugify(subscription.name) or "inbox"
        return f"subscription_{subscription.pk}_{slug}.csv"
