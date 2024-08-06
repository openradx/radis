from django.contrib import admin

from .models import Subscription, SubscriptionAppSettings

admin.site.register(SubscriptionAppSettings, admin.ModelAdmin)

admin.site.register(Subscription, admin.ModelAdmin)