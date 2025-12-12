"""radis URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path
from django.apps import apps

urlpatterns = [
    path("django-admin/", include("loginas.urls")),
    path("django-admin/", admin.site.urls),
    path("api-auth/", include("rest_framework.urls")),
    path("accounts/", include("adit_radis_shared.accounts.urls")),
    path("", include("radis.core.urls")),
    path("token-authentication/", include("adit_radis_shared.token_authentication.urls")),
    path("chats/", include("radis.chats.urls")),
    path("reports/", include("radis.reports.urls")),
    path("api/reports/", include("radis.reports.api.urls")),
    path("search/", include("radis.search.urls")),
    path("extractions/", include("radis.extractions.urls")),
    path("collections/", include("radis.collections.urls")),
    path("notes/", include("radis.notes.urls")),
    path("subscriptions/", include("radis.subscriptions.urls")),
]

# Some Django test runners force `DEBUG=False` even if the settings module enables it.
# If these apps/middlewares are installed, we must still include their URLs so
# templates can reverse them without raising `NoReverseMatch`.
if apps.is_installed("django_browser_reload"):
    urlpatterns = [
        path("__reload__/", include("django_browser_reload.urls")),
    ] + urlpatterns

if apps.is_installed("debug_toolbar"):
    urlpatterns = [
        path("__debug__/", include("debug_toolbar.urls")),
    ] + urlpatterns
