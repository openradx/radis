from django.contrib import admin

from .models import RagsearchAppSettings

admin.site.register(RagsearchAppSettings, admin.ModelAdmin)
