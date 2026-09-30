from django.urls import path

from .views import lab_remap_view, lab_run_view, lab_text_view, lab_view

urlpatterns = [
    path("", lab_view, name="labels_lab"),
    path("text/", lab_text_view, name="labels_lab_text"),
    path("run/", lab_run_view, name="labels_lab_run"),
    path("remap/", lab_remap_view, name="labels_lab_remap"),
]
