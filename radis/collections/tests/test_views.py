import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.collections.factories import CollectionFactory
from radis.reports.factories import LanguageFactory, ReportFactory


def create_test_report():
    """Create a report with a supported language."""
    language = LanguageFactory.create(code="en")
    return ReportFactory.create(language=language)


@pytest.mark.django_db
def test_collection_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    response = client.get("/collections/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_create_view_get(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    response = client.get("/collections/create/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_create_view_post(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    response = client.post("/collections/create/", {"name": "Test Collection"})
    assert response.status_code == 204


@pytest.mark.django_db
def test_collection_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_detail_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=other_user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/")
    assert response.status_code == 404  # Should not see other user's collections


@pytest.mark.django_db
def test_collection_update_view_get(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/update/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_update_view_post(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    client.force_login(user)

    response = client.post(
        f"/collections/{collection.pk}/update/", {"name": "Updated Collection Name"}
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_delete_view(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    client.force_login(user)

    response = client.post(f"/collections/{collection.pk}/delete/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_collection_export_view(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/export/")
    assert response.status_code == 200
    expected_content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response["Content-Type"] == expected_content_type


@pytest.mark.django_db
def test_collection_select_view(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(f"/collections/select/{report.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_count_badge_view(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(f"/collections/count-badge/{report.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collected_report_remove_view(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    report = create_test_report()
    collection.reports.add(report)
    client.force_login(user)

    response = client.post(f"/collections/{collection.pk}/remove/{report.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_collection_update_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=other_user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/update/")
    assert response.status_code == 404  # Should not be able to update other user's collections


@pytest.mark.django_db
def test_collection_delete_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=other_user)
    client.force_login(user)

    response = client.post(f"/collections/{collection.pk}/delete/")
    assert response.status_code == 404  # Should not be able to delete other user's collections


@pytest.mark.django_db
def test_collection_export_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=other_user)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/export/")
    assert response.status_code == 404  # Should not be able to export other user's collections


@pytest.mark.django_db
def test_collected_report_remove_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=other_user)
    report = create_test_report()
    collection.reports.add(report)
    client.force_login(user)

    response = client.post(f"/collections/{collection.pk}/remove/{report.pk}/")
    assert response.status_code == 404  # Should not be able to remove from other user's collections


@pytest.mark.django_db
def test_collection_with_reports(client: Client):
    """Test collection detail view with reports"""
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    report1 = create_test_report()
    report2 = create_test_report()
    collection.reports.add(report1, report2)
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/")
    assert response.status_code == 200
