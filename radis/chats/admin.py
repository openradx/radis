from django.contrib import admin

from .models import ChatsSettings

admin.site.register(ChatsSettings, admin.ModelAdmin)
