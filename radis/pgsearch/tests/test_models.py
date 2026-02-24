import pytest

from radis.pgsearch.models import EmbeddingBackfillJob


@pytest.mark.django_db
class TestEmbeddingBackfillJob:
    def test_progress_percent_zero_total(self):
        job = EmbeddingBackfillJob.objects.create(total_reports=0, processed_reports=0)
        assert job.progress_percent == 0

    def test_progress_percent_partial(self):
        job = EmbeddingBackfillJob.objects.create(total_reports=100, processed_reports=50)
        assert job.progress_percent == 50

    def test_progress_percent_complete(self):
        job = EmbeddingBackfillJob.objects.create(total_reports=100, processed_reports=100)
        assert job.progress_percent == 100

    def test_progress_percent_capped_at_100(self):
        job = EmbeddingBackfillJob.objects.create(total_reports=10, processed_reports=15)
        assert job.progress_percent == 100

    def test_is_cancelable_pending(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.PENDING,
        )
        assert job.is_cancelable is True

    def test_is_cancelable_in_progress(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
        )
        assert job.is_cancelable is True

    def test_is_not_cancelable_success(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.SUCCESS,
        )
        assert job.is_cancelable is False

    def test_is_not_cancelable_failure(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.FAILURE,
        )
        assert job.is_cancelable is False

    def test_is_not_cancelable_canceled(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.CANCELED,
        )
        assert job.is_cancelable is False

    def test_is_not_cancelable_canceling(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.CANCELING,
        )
        assert job.is_cancelable is False

    def test_is_active_pending(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.PENDING,
        )
        assert job.is_active is True

    def test_is_active_in_progress(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.IN_PROGRESS,
        )
        assert job.is_active is True

    def test_is_not_active_success(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.SUCCESS,
        )
        assert job.is_active is False

    def test_is_not_active_canceled(self):
        job = EmbeddingBackfillJob.objects.create(
            status=EmbeddingBackfillJob.Status.CANCELED,
        )
        assert job.is_active is False

    def test_str_representation(self):
        job = EmbeddingBackfillJob.objects.create()
        assert str(job) == f"EmbeddingBackfillJob [{job.pk}]"

    def test_default_status_is_pending(self):
        job = EmbeddingBackfillJob.objects.create()
        assert job.status == EmbeddingBackfillJob.Status.PENDING

    def test_ordering_by_created_at_desc(self):
        job1 = EmbeddingBackfillJob.objects.create()
        job2 = EmbeddingBackfillJob.objects.create()
        jobs = list(EmbeddingBackfillJob.objects.all())
        assert jobs[0].pk == job2.pk
        assert jobs[1].pk == job1.pk
