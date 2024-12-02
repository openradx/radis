from django.contrib import admin

from .models import ChatsSettings, Grammar

admin.site.register(ChatsSettings, admin.ModelAdmin)
admin.site.register(Grammar, admin.ModelAdmin)
