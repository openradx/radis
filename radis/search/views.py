from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest
from django.shortcuts import render
from django.views import View

from radis.search.forms import SearchForm
from radis.search.utils.query_parser import QueryParser

from .site import Search, SearchFilters, search_providers


class SearchView(LoginRequiredMixin, UserPassesTestMixin, View):
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs):
        form = SearchForm(request.GET)
        context: dict[str, Any] = {"form": form}

        if not form.is_valid():
            return render(request, "search/search.html", context)

        query = form.cleaned_data["query"]
        provider = form.cleaned_data["provider"]
        language = form.cleaned_data["language"]
        modalities = form.cleaned_data["modalities"]
        study_date_from = form.cleaned_data["study_date_from"]
        study_date_till = form.cleaned_data["study_date_till"]
        study_description = form.cleaned_data["study_description"]
        patient_sex = form.cleaned_data["patient_sex"]
        age_from = form.cleaned_data["age_from"]
        age_till = form.cleaned_data["age_till"]

        search_provider = search_providers[provider]
        context["selected_provider"] = search_provider.name

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

        query_node, fixes = QueryParser().parse(query)

        if query_node is not None:
            if len(fixes) > 0:
                context["fixed_query"] = QueryParser.unparse(query_node)

            search = Search(
                query=query_node,
                filters=SearchFilters(
                    group=active_group.pk,
                    language=language,
                    modalities=modalities,
                    study_date_from=study_date_from,
                    study_date_till=study_date_till,
                    study_description=study_description,
                    patient_sex=patient_sex,
                    patient_age_from=age_from,
                    patient_age_till=age_till,
                ),
                offset=offset,
                limit=page_size,
            )
            result = search_provider.search(search)
            total_count = result.total_count

            if total_count is not None:
                context["total_count"] = total_count
                context["total_relation"] = result.total_relation

                # We don't allow to paginate through all results, but the provider tells
                # us how many results it can return
                max_size = min(total_count, search_provider.max_results)
                paginator = Paginator(range(max_size), page_size)
                context["paginator"] = paginator
                context["page_obj"] = paginator.get_page(page_number)

            context["form"] = form
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
        page_size = request.GET.get("per_page") or 25
        try:
            page_size = min(int(page_size), 100)
        except ValueError:
            page_size = 10
        return page_size
