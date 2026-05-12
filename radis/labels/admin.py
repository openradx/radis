from django.contrib import admin

from .models import (
    Answer,
    AnswerOption,
    BackfillJob,
    EvalSample,
    LabelingRun,
    Question,
    QuestionSet,
)


class AnswerOptionInline(admin.TabularInline):
    model = AnswerOption
    extra = 0


@admin.register(QuestionSet)
class QuestionSetAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "order", "last_edited_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("order", "name")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("label", "question", "question_set", "is_active", "order", "version")
    list_filter = ("question_set", "is_active")
    search_fields = ("label", "question")
    ordering = ("question_set__order", "order", "label")
    inlines = (AnswerOptionInline,)


@admin.register(LabelingRun)
class LabelingRunAdmin(admin.ModelAdmin):
    list_display = ("id", "report", "question_set", "mode", "status", "created_at")
    list_filter = ("question_set", "mode", "status")
    search_fields = ("report__document_id",)
    ordering = ("-created_at",)


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("report", "question", "option", "confidence", "verified", "created_at")
    list_filter = ("verified", "question__question_set")
    search_fields = ("report__document_id", "question__label", "option__label")
    ordering = ("-created_at",)


@admin.register(EvalSample)
class EvalSampleAdmin(admin.ModelAdmin):
    list_display = ("name", "question_set", "target_size", "actual_size", "created_at")
    list_filter = ("question_set",)
    search_fields = ("name",)
    ordering = ("-created_at",)


@admin.register(BackfillJob)
class BackfillJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "question_set",
        "status",
        "processed_reports",
        "total_reports",
        "created_at",
    )
    list_filter = ("status",)
    ordering = ("-created_at",)
