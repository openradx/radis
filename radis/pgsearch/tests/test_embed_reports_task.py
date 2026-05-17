from unittest.mock import patch

import pytest

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import embed_reports as _embed_reports_task
from radis.reports.factories import ReportFactory

# Procrastinate's @app.task wraps the function; tests call the underlying
# function directly to skip the broker layer.
embed_reports = _embed_reports_task.__wrapped__  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_embed_reports_writes_normalized_vector():
    report = ReportFactory.create(body="Findings: no acute abnormality.")
    fake_vec = [1.0] + [0.0] * 1023  # already normalized

    with patch(
        "radis.pgsearch.tasks.EmbeddingClient"
    ) as MockClient:
        MockClient.return_value.embed_documents.return_value = [fake_vec]
        embed_reports([report.pk])

    rsv = ReportSearchVector.objects.get(report=report)
    assert rsv.embedding is not None
    assert len(rsv.embedding) == 1024
    assert pytest.approx(rsv.embedding[0]) == 1.0


@pytest.mark.django_db
def test_embed_reports_overwrites_existing_embedding():
    report = ReportFactory.create()
    rsv = ReportSearchVector.objects.get(report=report)
    rsv.embedding = [0.5] * 1024
    rsv.save(update_fields=["embedding"])

    new_vec = [1.0] + [0.0] * 1023
    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        MockClient.return_value.embed_documents.return_value = [new_vec]
        embed_reports([report.pk])

    rsv.refresh_from_db()
    assert pytest.approx(rsv.embedding[0]) == 1.0
    assert pytest.approx(rsv.embedding[1]) == 0.0


@pytest.mark.django_db
def test_embed_reports_skips_missing_ids_without_error():
    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        # No reports created. Should not call the client at all.
        embed_reports([99999])
        MockClient.return_value.embed_documents.assert_not_called()


@pytest.mark.django_db
def test_embed_reports_splits_into_batches(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    reports = [ReportFactory.create() for _ in range(5)]
    fake_vec = [1.0] + [0.0] * 1023

    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        MockClient.return_value.embed_documents.side_effect = [
            [fake_vec, fake_vec],
            [fake_vec, fake_vec],
            [fake_vec],
        ]
        embed_reports([r.pk for r in reports])

    assert MockClient.return_value.embed_documents.call_count == 3


@pytest.mark.django_db
def test_embed_reports_propagates_client_error():
    from radis.pgsearch.utils.embedding_client import EmbeddingClientError

    report = ReportFactory.create()
    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        MockClient.return_value.embed_documents.side_effect = EmbeddingClientError("boom")
        with pytest.raises(EmbeddingClientError):
            embed_reports([report.pk])


@pytest.mark.django_db
def test_embed_reports_closes_client_on_success():
    report = ReportFactory.create()
    fake_vec = [1.0] + [0.0] * 1023

    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        MockClient.return_value.embed_documents.return_value = [fake_vec]
        embed_reports([report.pk])

    MockClient.return_value.close.assert_called_once()


@pytest.mark.django_db
def test_embed_reports_closes_client_on_error():
    from radis.pgsearch.utils.embedding_client import EmbeddingClientError

    report = ReportFactory.create()
    with patch("radis.pgsearch.tasks.EmbeddingClient") as MockClient:
        MockClient.return_value.embed_documents.side_effect = EmbeddingClientError("boom")
        with pytest.raises(EmbeddingClientError):
            embed_reports([report.pk])
    MockClient.return_value.close.assert_called_once()
