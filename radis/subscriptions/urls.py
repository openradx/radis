from adit_radis_shared.common.views import HtmxTemplateView
from django.urls import path

from .views import (
    SubscriptionCreateView,
    SubscriptionDeleteView,
    SubscriptionInboxView,
    SubscriptionListView,
    SubscriptionUpdateView,
)

urlpatterns = [
    path("", SubscriptionListView.as_view(), name="subscription_list"),
    path("create/", SubscriptionCreateView.as_view(), name="subscription_create"),
    path("<int:pk>/update/", SubscriptionUpdateView.as_view(), name="subscription_update"),
    path("<int:pk>/delete/", SubscriptionDeleteView.as_view(), name="subscription_delete"),
    path(
        "help/",
        HtmxTemplateView.as_view(template_name="subscriptions/_subscription_help.html"),
        name="subscription_help",
    ),
    path("<int:pk>/inbox/", SubscriptionInboxView.as_view(), name="subscription_inbox"),
]
