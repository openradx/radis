from django.contrib import admin
from django.db.models import Count, Q
from django.utils.html import format_html

from .models import Question


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
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
        return format_html(
            "{yes} Yes · {maybe} Maybe · {no} No · {stale} stale", **counts
        )
    answer_summary.short_description = "Answers"
