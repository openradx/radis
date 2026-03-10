from django.contrib import admin

from .models import LabelBackfillJob, LabelChoice, LabelGroup, LabelQuestion, ReportLabel


class LabelChoiceInline(admin.TabularInline):
    model = LabelChoice
    extra = 0


@admin.register(LabelGroup)
class LabelGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "order")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("order", "name")


@admin.register(LabelQuestion)
class LabelQuestionAdmin(admin.ModelAdmin):
    list_display = ("label", "question", "group", "is_active", "order")
    list_filter = ("group", "is_active")
    search_fields = ("label", "question")
    ordering = ("group__order", "order", "label")
    inlines = (LabelChoiceInline,)


@admin.register(ReportLabel)
class ReportLabelAdmin(admin.ModelAdmin):
    list_display = ("report", "question", "choice", "confidence", "verified", "created_at")
    list_filter = ("verified", "question__group")
    search_fields = ("report__document_id", "question__name", "choice__label")
    ordering = ("-created_at",)


@admin.register(LabelBackfillJob)
class LabelBackfillJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label_group",
        "status",
        "processed_reports",
        "total_reports",
        "created_at",
    )
    list_filter = ("status",)
    ordering = ("-created_at",)
