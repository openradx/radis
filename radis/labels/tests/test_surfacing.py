import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.reports.factories import ReportFactory


def _user_with_active_group():
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()
    return user, group


def _render_detail(client: Client, report) -> str:
    response = client.get(f"/reports/{report.pk}/")
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.django_db
def test_only_surfacing_buckets_render(client: Client) -> None:
    """PRESENT labels render on the detail page; ABSENT labels never do."""
    user, group = _user_with_active_group()
    client.force_login(user)

    label_group = LabelGroupFactory.create(name="Chest")
    present = LabelFactory.create(group=label_group, name="edema")
    absent = LabelFactory.create(group=label_group, name="nodule")
    report = ReportFactory.create()
    report.groups.add(group)
    LabelResultFactory.create(report=report, label=present, value=LabelResult.Value.PRESENT)
    LabelResultFactory.create(report=report, label=absent, value=LabelResult.Value.ABSENT)

    html = _render_detail(client, report)

    assert "edema" in html
    assert "nodule" not in html


@pytest.mark.django_db
def test_surfacing_buckets_grouped_by_group_name(client: Client) -> None:
    """All three surfacing buckets render, grouped under their label group's name."""
    user, group = _user_with_active_group()
    client.force_login(user)

    label_group = LabelGroupFactory.create(name="Chest")
    likely = LabelFactory.create(group=label_group, name="effusion")
    possible = LabelFactory.create(group=label_group, name="atelectasis")
    report = ReportFactory.create()
    report.groups.add(group)
    LabelResultFactory.create(report=report, label=likely, value=LabelResult.Value.LIKELY)
    LabelResultFactory.create(report=report, label=possible, value=LabelResult.Value.POSSIBLE)

    html = _render_detail(client, report)

    assert "Chest" in html
    assert "effusion" in html
    assert "atelectasis" in html
