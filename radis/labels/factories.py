import factory
from factory.django import DjangoModelFactory

from .models import Question


class QuestionFactory(DjangoModelFactory):
    class Meta:
        model = Question

    text = factory.Faker("sentence")
    label = factory.Sequence(lambda n: f"label_{n}")
    group = "default"
    active = True
