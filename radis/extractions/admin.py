from django.contrib import admin

from .models import ExtractionInstance, ExtractionJob, ExtractionTask, OutputField


class OutputFieldInline(admin.StackedInline):
    model = OutputField
    extra = 1
    ordering = ("id",)


class ExtractionJobAdmin(admin.ModelAdmin):
    inlines = [OutputFieldInline]


admin.site.register(ExtractionJob, ExtractionJobAdmin)


class ExtractionInstanceInline(admin.StackedInline):
    model = ExtractionInstance
    extra = 1
    ordering = ("id",)


class ExtractionTaskAdmin(admin.ModelAdmin):
    inlines = [ExtractionInstanceInline]


admin.site.register(ExtractionTask, ExtractionTaskAdmin)
