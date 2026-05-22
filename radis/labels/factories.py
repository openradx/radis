import factory
from adit_radis_shared.accounts.factories import UserFactory
from factory.django import DjangoModelFactory

from radis.core.models import AnalysisJob
from radis.reports.factories import ReportFactory

from .models import Answer, LabelingJob, LabelingTask, Question


class QuestionFactory(DjangoModelFactory):
    class Meta:
        model = Question

    text = factory.Faker("sentence")
    label = factory.Sequence(lambda n: f"label_{n}")
    group = "default"
    active = True


class AnswerFactory(DjangoModelFactory):
    class Meta:
        model = Answer

    report = factory.SubFactory(ReportFactory)
    question = factory.SubFactory(QuestionFactory)
    value = "YES"


class LabelingJobFactory(DjangoModelFactory):
    class Meta:
        model = LabelingJob

    owner = factory.SubFactory(UserFactory)
    status = AnalysisJob.Status.UNVERIFIED


class LabelingTaskFactory(DjangoModelFactory):
    class Meta:
        model = LabelingTask

    job = factory.SubFactory(LabelingJobFactory)
