from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import BadRequest
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views import View

from radis.core.types import AuthenticatedRequest

from .models import ReportQuery
from .serializers import SearchParamsSerializer
from .rag import Rag


class RagsearchView(LoginRequiredMixin, View):
    def get(self, request: AuthenticatedRequest, *args, **kwargs):
        serializer = SearchParamsSerializer(data=request.GET)

        if not serializer.is_valid():
            raise BadRequest("Invalid GET parameters.")

        query: str = serializer.validated_data["query"]
        rag_request: str = serializer.validated_data["rag_request"]
        page_number: int = serializer.validated_data["page"]
        page_size: int = serializer.validated_data["per_page"]
        offset = (page_number - 1) * page_size

        context = {}
        if query and rag_request:
            result = ReportQuery.query_reports(query, offset, page_size)
            total_count = result.total_count
            paginator = Paginator(range(total_count), page_size)
            page = paginator.get_page(page_number)

            context["query"] = query
            context["offset"] = offset
            context["paginator"] = paginator
            context["page_obj"] = page
            context["total_count"] = total_count
            context["rag_request"] = rag_request
            # here goes the routine to sort out only the relevant reports using RAG
            rag = Rag(query=query, request=rag_request, reports=result.reports)
            extracted_reports = rag.get()
            context["reports"] = extracted_reports

        return render(request, "ragsearch/ragsearch.html", context)
