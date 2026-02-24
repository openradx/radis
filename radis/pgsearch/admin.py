from django.contrib import admin

from .models import EmbeddingBackfillJob


@admin.register(EmbeddingBackfillJob)
class EmbeddingBackfillJobAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "status",
        "total_reports",
        "processed_reports",
        "progress_percent",
        "created_at",
        "started_at",
        "ended_at",
    ]
    list_filter = ["status"]
    readonly_fields = [
        "total_reports",
        "processed_reports",
        "message",
        "created_at",
        "started_at",
        "ended_at",
    ]
