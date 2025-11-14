import pytest
from django.test import Client
from django.urls import reverse


class TestHealthView:
    @pytest.mark.django_db
    def test_health_endpoint_returns_ok(self, client: Client):
        response = client.get(reverse("health"))

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
