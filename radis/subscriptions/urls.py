from adit_radis_shared.common.views import HtmxTemplateView
from django.urls import path

from .views import (
    SubscriptionCreateView,
    SubscriptionDeleteView,
    SubscriptionDetailView,
    SubscriptionInboxView,
    SubscriptionListView,
)

urlpatterns = [
    path("create/", SubscriptionCreateView.as_view(), name="subscription_create"),
    path("list/", SubscriptionListView.as_view(), name="subscription_list"),
    path("<int:pk>/", SubscriptionDetailView.as_view(), name="subscription_detail"),
    path("<int:pk>/delete/", SubscriptionDeleteView.as_view(), name="subscription_delete"),
    path(
        "help/",
        HtmxTemplateView.as_view(template_name="subscriptions/_subscription_help.html"),
        name="subscription_help",
    ),
    path("<int:pk>/inbox/", SubscriptionInboxView.as_view(), name="subscription_inbox"),
]
