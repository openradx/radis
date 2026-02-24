from unittest.mock import MagicMock, patch

import pytest

from radis.pgsearch.models import EmbeddingBackfillJob, ReportSearchVector
from radis.pgsearch.tasks import (
    _increment_and_maybe_finalize,
    enqueue_embedding_backfill,
    generate_report_embedding,
    process_embedding_batch,
)
from radis.reports.factories import LanguageFactory, ReportFactory


@pytest.mark.django_db
class TestGenerateReportEmbedding:
    def test_generates_and_stores_embedding(self):
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Test report body")
        # The signal creates the ReportSearchVector, but we need to verify it exists
        assert ReportSearchVector.objects.filter(report=report).exists()

        mock_embedding = [0.1] * 10

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.embed_single.return_value = mock_embedding

            generate_report_embedding(report.pk)

            mock_client.embed_single.assert_called_once_with(report.body)

    def test_skips_nonexistent_report(self):
        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            generate_report_embedding(99999)
            mock_cls.assert_not_called()

    def test_logs_and_returns_on_api_error(self):
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Test body")

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.embed_single.side_effect = Exception("API error")

            # Should not raise
            generate_report_embedding(report.pk)


@pytest.mark.django_db
class TestProcessEmbeddingBatch:
    def test_processes_batch_without_backfill_job(self):
        language = LanguageFactory.create(code="en")
        report1 = ReportFactory.create(language=language, body="Body one")
        report2 = ReportFactory.create(language=language, body="Body two")

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.embed.return_value = [[0.1, 0.2], [0.3, 0.4]]

            process_embedding_batch(report_ids=[report1.pk, report2.pk])

            mock_client.embed.assert_called_once()
            texts_arg = mock_client.embed.call_args[0][0]
            assert len(texts_arg) == 2

    def test_increments_backfill_job_progress(self):
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Test body")

        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
            total_reports=1,
        )

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.embed.return_value = [[0.1, 0.2]]

            process_embedding_batch(
                report_ids=[report.pk],
                backfill_job_id=job.id,
            )

        job.refresh_from_db()
        assert job.processed_reports == 1
        assert job.status == EmbeddingBackfillJob.Status.SUCCESS
        assert job.ended_at is not None

    def test_skips_canceled_backfill_job(self):
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Test body")

        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.CANCELING,
            total_reports=1,
        )

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            process_embedding_batch(
                report_ids=[report.pk],
                backfill_job_id=job.id,
            )
            mock_cls.assert_not_called()

        job.refresh_from_db()
        assert job.processed_reports == 1
        assert job.status == EmbeddingBackfillJob.Status.CANCELED

    def test_skips_nonexistent_backfill_job(self):
        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            process_embedding_batch(
                report_ids=[1, 2],
                backfill_job_id=99999,
            )
            mock_cls.assert_not_called()

    def test_handles_empty_report_list(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
            total_reports=5,
        )

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            # Pass IDs that don't exist in the DB
            process_embedding_batch(
                report_ids=[99998, 99999],
                backfill_job_id=job.id,
            )
            mock_cls.assert_not_called()

        job.refresh_from_db()
        assert job.processed_reports == 2

    def test_increments_progress_on_api_error(self):
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Test body")

        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
            total_reports=1,
        )

        with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.embed.side_effect = Exception("API error")

            process_embedding_batch(
                report_ids=[report.pk],
                backfill_job_id=job.id,
            )

        job.refresh_from_db()
        assert job.processed_reports == 1


@pytest.mark.django_db
class TestIncrementAndMaybeFinalize:
    def test_finalizes_in_progress_job_as_success(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
            total_reports=10,
            processed_reports=8,
        )

        _increment_and_maybe_finalize(job.id, 2)

        job.refresh_from_db()
        assert job.processed_reports == 10
        assert job.status == EmbeddingBackfillJob.Status.SUCCESS
        assert job.ended_at is not None

    def test_finalizes_canceling_job_as_canceled(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.CANCELING,
            total_reports=10,
            processed_reports=8,
        )

        _increment_and_maybe_finalize(job.id, 2)

        job.refresh_from_db()
        assert job.processed_reports == 10
        assert job.status == EmbeddingBackfillJob.Status.CANCELED
        assert job.ended_at is not None

    def test_does_not_finalize_when_not_complete(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
            total_reports=10,
            processed_reports=3,
        )

        _increment_and_maybe_finalize(job.id, 2)

        job.refresh_from_db()
        assert job.processed_reports == 5
        assert job.status == EmbeddingBackfillJob.Status.IN_PROGRESS
        assert job.ended_at is None

    def test_handles_nonexistent_job(self):
        # Should not raise
        _increment_and_maybe_finalize(99999, 5)


@pytest.mark.django_db
class TestEnqueueEmbeddingBackfill:
    def test_enqueues_batches_for_reports_without_embeddings(self, settings):
        settings.EMBEDDING_BACKFILL_TASK_BATCH_SIZE = 2

        language = LanguageFactory.create(code="en")
        ReportFactory.create(language=language, body="Body 1")
        ReportFactory.create(language=language, body="Body 2")
        ReportFactory.create(language=language, body="Body 3")

        job = EmbeddingBackfillJob.objects.create()
        deferred_calls = []

        with patch(
            "radis.pgsearch.tasks.process_embedding_batch.defer",
            side_effect=lambda **kwargs: deferred_calls.append(kwargs),
        ):
            enqueue_embedding_backfill(backfill_job_id=job.id)

        job.refresh_from_db()
        assert job.status == EmbeddingBackfillJob.Status.IN_PROGRESS
        assert job.total_reports == 3
        assert job.started_at is not None
        # 3 reports with batch size 2 -> 2 batches
        assert len(deferred_calls) == 2

    def test_completes_immediately_when_no_reports(self):
        job = EmbeddingBackfillJob.objects.create()

        with patch(
            "radis.pgsearch.tasks.process_embedding_batch.defer",
        ) as mock_defer:
            enqueue_embedding_backfill(backfill_job_id=job.id)

        job.refresh_from_db()
        assert job.status == EmbeddingBackfillJob.Status.SUCCESS
        assert job.message == "No reports to process."
        assert job.ended_at is not None
        mock_defer.assert_not_called()

    def test_handles_nonexistent_job(self):
        with patch(
            "radis.pgsearch.tasks.process_embedding_batch.defer",
        ) as mock_defer:
            # Should not raise
            enqueue_embedding_backfill(backfill_job_id=99999)
            mock_defer.assert_not_called()

    def test_force_reprocesses_all_reports(self, settings):
        settings.EMBEDDING_BACKFILL_TASK_BATCH_SIZE = 100

        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language, body="Body")
        # Simulate that the report already has an embedding
        ReportSearchVector.objects.filter(report=report).update(embedding=[0.1] * 10)

        job = EmbeddingBackfillJob.objects.create()
        deferred_calls = []

        with patch(
            "radis.pgsearch.tasks.process_embedding_batch.defer",
            side_effect=lambda **kwargs: deferred_calls.append(kwargs),
        ):
            enqueue_embedding_backfill(backfill_job_id=job.id, force=True)

        job.refresh_from_db()
        assert job.total_reports >= 1  # At least the one we created
        assert len(deferred_calls) >= 1
