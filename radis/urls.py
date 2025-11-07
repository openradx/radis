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

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

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


# Debug Toolbar in Debug mode only
if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__reload__/", include("django_browser_reload.urls")),
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns
