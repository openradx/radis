from django import forms
from django.contrib import admin
from django.db.models import Count, F, Q
from django.urls import path
from django.utils.html import format_html

from radis.core.models import AnalysisTask

from . import admin_views
from .models import Answer, LabelingJob, Question


class _GroupDatalistTextInput(forms.TextInput):
    """TextInput with an HTML <datalist> populated from existing Question.group values."""

    template_name = "labels/admin/widgets/group_datalist.html"

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        ctx["widget"]["list_id"] = "label-groups-datalist"
        ctx["widget"]["existing_groups"] = list(
            Question.objects.order_by("group").values_list("group", flat=True).distinct()
        )
        # Add list= attribute to the input
        ctx["widget"]["attrs"]["list"] = "label-groups-datalist"
        return ctx


class QuestionAdminForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ("label", "group", "text", "active")
        widgets = {
            "group": _GroupDatalistTextInput,
        }


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    form = QuestionAdminForm
    list_display = ("label", "group", "active", "text_preview", "updated_at", "answer_summary")
    list_filter = ("active", "group")
    search_fields = ("label", "group", "text")
    ordering = ("group", "label")
    readonly_fields = ("created_at", "updated_at", "answer_summary")
    fieldsets = (
        (None, {"fields": ("label", "group", "text", "active")}),
        ("Stats", {"fields": ("answer_summary", "created_at", "updated_at")}),
    )

    def text_preview(self, obj: Question) -> str:
        if not obj.text:
            return ""
        return obj.text[:80] + ("…" if len(obj.text) > 80 else "")

    text_preview.short_description = "Question"

    def answer_summary(self, obj: Question) -> str:
        if obj.pk is None:
            return "—"
        counts = obj.answers.aggregate(
            yes=Count("pk", filter=Q(value="YES")),
            no=Count("pk", filter=Q(value="NO")),
            maybe=Count("pk", filter=Q(value="MAYBE")),
            stale=Count("pk", filter=Q(generated_at__lt=obj.updated_at)),
        )
        return format_html("{yes} Yes · {maybe} Maybe · {no} No · {stale} stale", **counts)

    answer_summary.short_description = "Answers"


class IsStaleFilter(admin.SimpleListFilter):
    title = "stale"
    parameter_name = "is_stale"

    def lookups(self, request, model_admin):
        return [("1", "Stale"), ("0", "Current")]

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.filter(generated_at__lt=F("question__updated_at"))
        if self.value() == "0":
            return queryset.filter(generated_at__gte=F("question__updated_at"))
        return queryset


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("report", "question_label", "value", "is_stale", "generated_at")
    list_filter = ("value", "question__group", "question", IsStaleFilter)
    search_fields = ("report__document_id", "question__label")
    raw_id_fields = ("report", "question")
    readonly_fields = tuple(f.name for f in Answer._meta.fields)

    def question_label(self, obj: Answer) -> str:
        return obj.question.label

    question_label.short_description = "Label"

    def is_stale(self, obj: Answer) -> bool:
        return obj.generated_at < obj.question.updated_at

    is_stale.boolean = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AnswerInline(admin.TabularInline):
    model = Answer
    fields = ("question", "value", "generated_at", "is_stale")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False

    def is_stale(self, obj: Answer) -> bool:
        return obj.generated_at < obj.question.updated_at

    is_stale.boolean = True


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "owner",
        "progress_detail",
        "created_at",
        "started_at",
        "ended_at",
    )
    list_filter = ("status",)
    readonly_fields = (
        "status",
        "owner",
        "message",
        "created_at",
        "started_at",
        "ended_at",
        "progress_detail",
    )
    fields = readonly_fields
    change_list_template = "labels/admin/labelingjob_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "run/",
                self.admin_site.admin_view(admin_views.run_backfill_view),
                name="labels_run_backfill",
            ),
            path(
                "<int:job_id>/cancel/",
                self.admin_site.admin_view(admin_views.cancel_backfill_view),
                name="labels_cancel_backfill",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["active_job"] = LabelingJob.objects.filter(
            status__in=LabelingJob.ACTIVE_STATUSES
        ).first()
        return super().changelist_view(request, extra_context=extra_context)

    def progress_detail(self, obj: LabelingJob) -> str:
        if obj.pk is None:
            return "—"
        statuses = list(obj.tasks.values_list("status", flat=True))
        total = len(statuses)
        if total == 0:
            return "No tasks yet."
        s = sum(1 for x in statuses if x == AnalysisTask.Status.SUCCESS)
        w = sum(1 for x in statuses if x == AnalysisTask.Status.WARNING)
        f = sum(1 for x in statuses if x == AnalysisTask.Status.FAILURE)
        p = sum(1 for x in statuses if x == AnalysisTask.Status.PENDING)
        ip = sum(1 for x in statuses if x == AnalysisTask.Status.IN_PROGRESS)
        return (
            f"{s} success · {w} warning · {f} failure · "
            f"{ip} in-progress · {p} pending · {total} total"
        )

    def has_add_permission(self, request):
        return False
