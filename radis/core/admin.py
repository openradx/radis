from django.contrib import admin

from .models import CoreSettings, Report

admin.site.site_header = "RADIS administration"


admin.site.register(CoreSettings, admin.ModelAdmin)

admin.site.register(Report, admin.ModelAdmin)
