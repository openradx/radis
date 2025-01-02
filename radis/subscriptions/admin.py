from django.contrib import admin

from .models import Subscription, SubscriptionsAppSettings

admin.site.register(SubscriptionsAppSettings, admin.ModelAdmin)

admin.site.register(Subscription, admin.ModelAdmin)
