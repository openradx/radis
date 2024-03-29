from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest
from django.shortcuts import render
from django.views import View

from radis.core.mixins import HtmxOnlyMixin
from radis.core.types import AuthenticatedApiRequest, AuthenticatedHttpRequest
from radis.search.forms import SearchForm

from .site import Search, SearchFilters, search_providers


class SearchView(LoginRequiredMixin, View):
    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs):
        form = SearchForm(request.GET)
        context: dict[str, Any] = {"form": form}

        if not form.is_valid():
            return render(request, "search/search.html", context)

        query = form.cleaned_data["query"]
        provider = form.cleaned_data["provider"]
        study_date_from = form.cleaned_data["study_date_from"]
        study_date_till = form.cleaned_data["study_date_till"]
        study_description = form.cleaned_data["study_description"]
        modalities = form.cleaned_data["modalities"]
        patient_sex = form.cleaned_data["patient_sex"]
        age_from = form.cleaned_data["age_from"]
        age_till = form.cleaned_data["age_till"]

        search_provider = search_providers[provider]
        context["info_template"] = search_provider.info_template

        page_number = self.get_page_number(request)
        page_size: int = self.get_page_size(request)

        if page_size * page_number > search_provider.max_results:
            # https://github.com/django/django/blob/6f7c0a4d66f36c59ae9eafa168b455e462d81901/django/views/generic/list.py#L76
            raise Http404(f"Invalid page {page_number}.")

        offset = (page_number - 1) * page_size
        context["offset"] = offset

        if query:
            search = Search(
                query=query,
                offset=offset,
                limit=page_size,
                filters=SearchFilters(
                    study_date_from=study_date_from,
                    study_date_till=study_date_till,
                    study_description=study_description,
                    modalities=modalities,
                    patient_sex=patient_sex,
                    patient_age_from=age_from,
                    patient_age_till=age_till,
                ),
            )
            result = search_provider.handler(search)
            total_count = result.total_count

            if total_count is not None:
                context["total_count"] = total_count
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


class InfoView(LoginRequiredMixin, HtmxOnlyMixin, View):
    def post(self, request: AuthenticatedApiRequest, *args, **kwargs):
        provider_name = request.POST.get("provider", "")
        provider = search_providers.get(provider_name)

        if not provider:
            raise ValidationError(f"Invalid search provider: {provider_name}")

        return render(
            request,
            "search/_search_info.html",
            {
                "selected_provider": provider_name,
                "info_template": provider.info_template,
            },
        )
