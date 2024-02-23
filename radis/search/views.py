from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import BadRequest, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views import View

from radis.core.mixins import HtmxOnlyMixin
from radis.core.types import AuthenticatedApiRequest
from radis.search.forms import SearchForm

from .serializers import SearchParamsSerializer
from .site import Search, SearchHandler, search_handlers


class SearchView(LoginRequiredMixin, View):
    def get(self, request: AuthenticatedApiRequest, *args, **kwargs):
        serializer = SearchParamsSerializer(data=request.GET)
        form = SearchForm(request.GET)
        if form.is_valid():
            print(form.cleaned_data)
        else:
            print(form.errors)

        if not serializer.is_valid():
            raise BadRequest("Invalid GET parameters.")

        query: str = serializer.validated_data["query"]
        algorithm: str = serializer.validated_data["algorithm"]
        page_number: int = serializer.validated_data["page"]
        page_size: int = serializer.validated_data["per_page"]
        offset = (page_number - 1) * page_size

        search_handler: SearchHandler | None = None

        available_algorithms = sorted(list(search_handlers.keys()))
        context: dict[str, Any] = {"form": form, "available_algorithms": available_algorithms}

        if available_algorithms and algorithm:
            search_handler = search_handlers.get(algorithm)
            if search_handler:
                context["selected_algorithm"] = algorithm
                context["info_template_name"] = search_handler.info_template_name

        if available_algorithms and not search_handler:
            algorithm = available_algorithms[0]
            search_handler = search_handlers[available_algorithms[0]]
            context["selected_algorithm"] = algorithm
            context["info_template_name"] = search_handler.info_template_name

        if query and search_handler:
            search = Search(query=query, offset=offset, page_size=page_size)
            result = search_handler.searcher(search)
            total_count = result.total_count

            if total_count is not None:
                context["total_count"] = total_count
                paginator = Paginator(range(total_count), page_size)
                context["paginator"] = paginator
                context["page_obj"] = paginator.get_page(page_number)

            context["form"] = form
            context["query"] = query
            context["offset"] = offset
            context["documents"] = result.documents

        return render(request, "search/search.html", context)


class InfoView(LoginRequiredMixin, HtmxOnlyMixin, View):
    def post(self, request: AuthenticatedApiRequest, *args, **kwargs):
        algorithm = request.POST.get("algorithm", "")
        search_handler = search_handlers.get(algorithm)

        if not search_handler:
            raise ValidationError(f"Invalid search algorithm: {algorithm}")

        return render(
            request,
            "search/_search_info.html",
            {
                "selected_algorithm": algorithm,
                "info_template_name": search_handler.info_template_name,
            },
        )
