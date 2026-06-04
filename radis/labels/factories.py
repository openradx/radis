import factory

from radis.reports.factories import ReportFactory

from .models import GateAnswer, Label, LabelGroup, LabelResult


class BaseDjangoModelFactory[T](factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class LabelGroupFactory(BaseDjangoModelFactory[LabelGroup]):
    class Meta:
        model = LabelGroup

    name = factory.Sequence(lambda n: f"Group {n}")
    gate_question = factory.Faker("sentence")


class LabelFactory(BaseDjangoModelFactory[Label]):
    class Meta:
        model = Label

    group = factory.SubFactory(LabelGroupFactory)
    name = factory.Sequence(lambda n: f"label-{n}")
    description = factory.Faker("sentence")
    active = True


class LabelResultFactory(BaseDjangoModelFactory[LabelResult]):
    class Meta:
        model = LabelResult

    report = factory.SubFactory(ReportFactory)
    label = factory.SubFactory(LabelFactory)
    value = LabelResult.Value.PRESENT


class GateAnswerFactory(BaseDjangoModelFactory[GateAnswer]):
    class Meta:
        model = GateAnswer

    report = factory.SubFactory(ReportFactory)
    label_group = factory.SubFactory(LabelGroupFactory)
    value = GateAnswer.Value.YES
