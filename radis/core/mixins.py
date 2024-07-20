from typing import Any, Protocol

from adit_radis_shared.common.mixins import ViewProtocol
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models.query import QuerySet
from django.http import HttpRequest


# TODO: Move this to adit_radis_shared package. PR: https://github.com/openradx/adit-radis-shared/pull/5
class RelatedPaginationMixinProtocol(ViewProtocol, Protocol):
    request: HttpRequest
    object_list: QuerySet
    paginate_by: int

    def get_object(self) -> Any: ...

    def get_context_data(self, **kwargs) -> dict[str, Any]: ...

    def get_related_queryset(self) -> QuerySet: ...


class RelatedPaginationMixin:
    """This mixin provides pagination for a related queryset. This makes it possible to
    paginate a related queryset in a DetailView. The related queryset is obtained by
    the `get_related_queryset()` method that must be implemented by the subclass.
    If used in combination with `RelatedFilterMixin`, the `RelatedPaginationMixin` must be
    inherited first."""

    def get_related_queryset(self: RelatedPaginationMixinProtocol) -> QuerySet:
        raise NotImplementedError("You must implement this method")

    def get_context_data(self: RelatedPaginationMixinProtocol, **kwargs):
        context = super().get_context_data(**kwargs)

        if "object_list" in context:
            queryset = context["object_list"]
        else:
            queryset = self.get_related_queryset()

        paginator = Paginator(queryset, self.paginate_by)
        page = self.request.GET.get("page")

        if page is None:
            page = 1

        try:
            paginated_queryset = paginator.page(page)
        except PageNotAnInteger:
            paginated_queryset = paginator.page(1)
        except EmptyPage:
            paginated_queryset = paginator.page(paginator.num_pages)

        context["object_list"] = paginated_queryset
        context["paginator"] = paginator
        context["is_paginated"] = paginated_queryset.has_other_pages()
        context["page_obj"] = paginated_queryset

        return context
