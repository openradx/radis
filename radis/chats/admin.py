from django.contrib import admin

from .models import ChatsAppSettings

admin.site.register(ChatsAppSettings, admin.ModelAdmin)
