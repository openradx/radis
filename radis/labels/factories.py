import factory
from factory.django import DjangoModelFactory

from radis.reports.factories import ReportFactory

from .models import Answer, Question


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
