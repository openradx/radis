from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Prefetch, QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView
from django_tables2 import SingleTableView

from .forms import LabelChoiceFormSet, LabelGroupForm, LabelQuestionForm
from .models import LabelChoice, LabelGroup, LabelQuestion
from .tables import LabelGroupTable


class LabelGroupListView(LoginRequiredMixin, SingleTableView):
    model = LabelGroup
    table_class = LabelGroupTable
    template_name = "labels/label_group_list.html"
    paginate_by = 30
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[LabelGroup]:
        return LabelGroup.objects.all().order_by("order", "name")


class LabelGroupDetailView(LoginRequiredMixin, DetailView):
    model = LabelGroup
    template_name = "labels/label_group_detail.html"

    def get_queryset(self) -> QuerySet[LabelGroup]:
        return LabelGroup.objects.prefetch_related(
            Prefetch(
                "questions",
                queryset=LabelQuestion.objects.prefetch_related("choices").order_by(
                    "order", "name"
                ),
            )
        )


class LabelGroupCreateView(LoginRequiredMixin, CreateView):
    template_name = "labels/label_group_form.html"
    form_class = LabelGroupForm
    success_url = reverse_lazy("label_group_list")


class LabelGroupUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "labels/label_group_form.html"
    form_class = LabelGroupForm
    model = LabelGroup

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.object.pk})


class LabelGroupDeleteView(LoginRequiredMixin, DeleteView):
    model = LabelGroup
    success_url = reverse_lazy("label_group_list")
    template_name = "labels/label_group_confirm_delete.html"


class LabelQuestionCreateView(LoginRequiredMixin, CreateView):
    template_name = "labels/label_question_form.html"
    form_class = LabelQuestionForm
    model = LabelQuestion
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx["formset"] = LabelChoiceFormSet(self.request.POST, prefix="choices")
        else:
            ctx["formset"] = LabelChoiceFormSet(prefix="choices")
        ctx["group"] = self.group
        return ctx

    def form_valid(self, form) -> HttpResponse:
        ctx = self.get_context_data()
        formset = ctx["formset"]
        if formset.is_valid():
            form.instance.group = self.group
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            return HttpResponseRedirect(self.get_success_url())
        return self.form_invalid(form)

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})


class LabelQuestionUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "labels/label_question_form.html"
    form_class = LabelQuestionForm
    model = LabelQuestion
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[LabelQuestion]:
        return LabelQuestion.objects.filter(group=self.group).prefetch_related("choices")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx["formset"] = LabelChoiceFormSet(
                self.request.POST, instance=self.object, prefix="choices"
            )
        else:
            ctx["formset"] = LabelChoiceFormSet(instance=self.object, prefix="choices")
            ctx["formset"].extra = 0
        ctx["group"] = self.group
        return ctx

    def form_valid(self, form) -> HttpResponse:
        ctx = self.get_context_data()
        formset = ctx["formset"]
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            return HttpResponseRedirect(self.get_success_url())
        return self.form_invalid(form)

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})


class LabelQuestionDeleteView(LoginRequiredMixin, DeleteView):
    model = LabelQuestion
    template_name = "labels/label_question_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[LabelQuestion]:
        return LabelQuestion.objects.filter(group=self.group)

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})
