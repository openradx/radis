from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest
from django.shortcuts import render
from django.views import View

from radis.search.forms import SearchForm
from radis.search.utils.query_parser import QueryParser

from .site import FullTextSearch, SearchRequest, SemanticSearch, SimilarityRerank, search_provider

PAGE_SIZES = [10, 25, 50]
DEFAULT_PAGE_SIZE = 25
FULL_TEXT_NEEDS_KEYWORDS = (
    "Full-text mode needs keywords. Switch to Semantic to search by description alone."
)
SEMANTIC_NEEDS_DESCRIPTION = "Semantic mode needs a description."


class SearchView(LoginRequiredMixin, UserPassesTestMixin, View):
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs):
        if search_provider is None:
            raise ImproperlyConfigured("Search provider is not configured.")

        form = SearchForm(request.GET, semantic=search_provider.semantic)
        context: dict[str, Any] = {"form": form, "page_sizes": PAGE_SIZES}

        if not form.is_valid():
            return render(request, "search/search.html", context)

        page_number = self.get_page_number(request)
        page_size: int = self.get_page_size(request)

        if page_size * page_number > search_provider.max_results:
            # https://github.com/django/django/blob/6f7c0a4d66f36c59ae9eafa168b455e462d81901/django/views/generic/list.py#L76
            raise Http404(f"Invalid page {page_number}.")

        offset = (page_number - 1) * page_size
        context["offset"] = offset

        active_group = self.request.user.active_group

        # TODO: when no active group is selected show user a error
        assert active_group

        semantic_mode = form.semantic_mode()
        description = form.cleaned_data.get("description", "")
        context["mode"] = "semantic" if semantic_mode else "fulltext"
        context["description"] = description

        search: SearchRequest
        if semantic_mode:
            if not description:
                context["mode_notice"] = SEMANTIC_NEEDS_DESCRIPTION
                return render(request, "search/search.html", context)
            search = SemanticSearch(
                text=description,
                min_similarity=form.cleaned_data.get("min_similarity"),
                filters=form.to_filters(active_group.pk),
                offset=offset,
                limit=page_size,
            )
            cutoff = search.min_similarity
            if cutoff is None:
                cutoff = settings.SEARCH_SEMANTIC_MIN_SIMILARITY
            context["cutoff"] = cutoff
        else:
            query_node, fixes = QueryParser().parse(form.cleaned_data["query"])
            if query_node is None:
                if description:
                    context["mode_notice"] = FULL_TEXT_NEEDS_KEYWORDS
                return render(request, "search/search.html", context)
            if len(fixes) > 0:
                context["fixed_query"] = QueryParser.unparse(query_node)
            search = FullTextSearch(
                query=query_node,
                rerank=SimilarityRerank(description) if description else None,
                filters=form.to_filters(active_group.pk),
                offset=offset,
                limit=page_size,
            )

        result = search_provider.search(search)
        context["total_count"] = result.total_count
        context["total_relation"] = result.total_relation
        context["degraded"] = result.degraded
        context["reranked"] = bool(description) and not semantic_mode and not result.degraded
        context["window"] = settings.SEARCH_RERANK_WINDOW
        context["weight"] = settings.SEARCH_RERANK_SEMANTIC_WEIGHT
        context["weight_complement"] = round(1.0 - settings.SEARCH_RERANK_SEMANTIC_WEIGHT, 2)

        # We don't allow to paginate through all results, but the provider tells
        # us how many results it can return
        max_size = min(result.total_count, search_provider.max_results)
        paginator = Paginator(range(max_size), page_size)
        context["paginator"] = paginator
        context["page_obj"] = paginator.get_page(page_number)
        context["documents"] = result.documents

        return render(request, "search/search.html", context)

    def get_page_number(self, request: HttpRequest) -> int:
        page = request.GET.get("page") or 1
        try:
            page_number = max(int(page), 1)
        except ValueError:
            page_number = 1
        return page_number

    def get_page_size(self, request: HttpRequest) -> int:
        page_size = request.GET.get("per_page") or DEFAULT_PAGE_SIZE
        try:
            page_size = min(int(page_size), 100)
        except ValueError:
            page_size = DEFAULT_PAGE_SIZE
        return page_size if page_size >= 1 else DEFAULT_PAGE_SIZE
