import pytest
from django.db import IntegrityError, transaction

from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory


class TestLabelingJobSingleton:
    @pytest.mark.parametrize(
        "first_status",
        [
            AnalysisJob.Status.UNVERIFIED,
            AnalysisJob.Status.PREPARING,
            AnalysisJob.Status.PENDING,
            AnalysisJob.Status.IN_PROGRESS,
            AnalysisJob.Status.CANCELING,
        ],
    )
    def test_blocks_second_active_job(self, first_status):
        LabelingJobFactory(status=first_status)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabelingJobFactory(status=AnalysisJob.Status.PENDING)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AnalysisJob.Status.SUCCESS,
            AnalysisJob.Status.WARNING,
            AnalysisJob.Status.FAILURE,
            AnalysisJob.Status.CANCELED,
        ],
    )
    def test_allows_new_after_terminal(self, terminal_status):
        first = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        first.status = terminal_status
        first.save()
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
