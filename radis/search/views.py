from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, SuspiciousOperation
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from django.views.decorators.http import require_POST
from django_htmx.http import HttpResponseClientRedirect
from pyparsing import ParseBaseException

from radis.search.forms import DESCRIPTION_MAX_LENGTH, DescriptionRequestForm, SearchForm
from radis.search.utils.description import write_description
from radis.search.utils.query_parser import QueryParser

from .site import FullTextSearch, SearchRequest, SemanticSearch, SimilarityRerank, search_provider

PAGE_SIZES = [10, 25, 50]
DEFAULT_PAGE_SIZE = 25
FULL_TEXT_NEEDS_KEYWORDS = (
    "Full-text mode needs keywords. Switch to Semantic to search by description alone."
)
SEMANTIC_NEEDS_DESCRIPTION = "Semantic mode needs a description."
ENTER_KEYWORDS = "Enter search keywords first; the description is written from the positive terms."
WRITER_FAILED = "The LLM could not write a description right now."


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


async def _description_response(request: HttpRequest, value: str, notice: str) -> HttpResponse:
    # Context processors may touch the sync ORM or session APIs, so the render runs
    # off the event loop.
    return await sync_to_async(render)(
        request,
        "search/form_elements/description_input.html",
        {"value": value, "notice": notice},
    )


@require_POST
async def description_view(request: AuthenticatedHttpRequest) -> HttpResponse:
    """HTMX endpoint behind "Write from keywords": re-renders the description row
    with an LLM-written passage about the positive keywords, or with a notice.

    Async so the LLM call does not occupy a worker thread. The query parse is pushed
    off the event loop because its run time grows exponentially with parenthesis
    nesting. The body touches only ``request.auser()`` and an ORM-free form; a sync
    database call here would raise ``SynchronousOnlyOperation`` under ASGI.
    """
    if not request.htmx:
        raise SuspiciousOperation

    user = await request.auser()
    if not user.is_authenticated:
        # The XHR would follow a plain redirect, and the login page has no textarea for
        # hx-select to keep, so the swap would delete it with the draft; HX-Redirect
        # makes HTMX load the login page as a whole page instead.
        next_url = request.htmx.current_url_abs_path or reverse("search")
        return HttpResponseClientRedirect(redirect_to_login(next_url).url)
    if user.active_group_id is None:  # type: ignore[attr-defined]
        raise PermissionDenied

    form = DescriptionRequestForm(request.POST)
    submitted = request.POST.get("description", "")[:DESCRIPTION_MAX_LENGTH]
    if not form.is_valid():
        return await _description_response(request, submitted, WRITER_FAILED)
    submitted = form.cleaned_data["description"]

    try:
        node, _fixes = await sync_to_async(QueryParser().parse)(form.cleaned_data["query"])
    except ParseBaseException:
        # The parser's repairs leave some inputs unparseable, e.g. an operator left
        # dangling beside an empty group.
        node = None
    terms = QueryParser.unparse_positive_terms(node) if node is not None else ""
    if not terms.strip():
        return await _description_response(request, submitted, ENTER_KEYWORDS)

    description = await write_description(terms)
    if description is None:
        return await _description_response(request, submitted, WRITER_FAILED)
    return await _description_response(request, description[:DESCRIPTION_MAX_LENGTH], "")
