from django.contrib import admin

from .models import Language, Metadata, Modality, Report, ReportsAppSettings

admin.site.register(ReportsAppSettings, admin.ModelAdmin)

admin.site.register(Language, admin.ModelAdmin)

admin.site.register(Modality, admin.ModelAdmin)


class MetadataInline(admin.TabularInline):
    model = Metadata
    extra = 1
    ordering = ("key",)


class ReportAdmin(admin.ModelAdmin):
    inlines = [MetadataInline]


admin.site.register(Report, ReportAdmin)
