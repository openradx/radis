from typing import Generic, TypeVar, cast

import factory
from faker import Faker

from radis.reports.factories import ModalityFactory

from .models import Answer, Question, RagInstance, RagJob, RagTask
from .site import retrieval_providers

T = TypeVar("T")

fake = Faker()

MODALITIES = ("CT", "MR", "DX", "PT", "US")


class BaseDjangoModelFactory(Generic[T], factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class RagJobFactory(BaseDjangoModelFactory):
    class Meta:
        model = RagJob

    title = factory.Faker("sentence", nb_words=3)
    provider = factory.Faker("random_element", elements=list(retrieval_providers.keys()))
    group = factory.SubFactory("adit_radis_shared.accounts.factories.GroupFactory")
    query = factory.Faker("word")
    language = factory.SubFactory("radis.reports.factories.LanguageFactory")
    study_date_from = factory.Faker("date")
    study_date_till = factory.Faker("date")
    study_description = factory.Faker("sentence", nb_words=5)
    patient_sex = factory.Faker("random_element", elements=["M", "F", "O"])
    age_from = factory.Faker("random_int", min=0, max=100)
    age_till = factory.Faker("random_int", min=0, max=100)

    @factory.post_generation
    def modalities(self, create, extracted, **kwargs):
        """
        If called like: ReportFactory.create(modalities=["CT", "PT"]) it generates
        a report with 2 modalities. If called without `modalities` argument, it
        generates a random amount of modalities for the report.
        """
        if not create:
            return

        modalities = extracted
        if modalities is None:
            modalities = fake.random_elements(elements=MODALITIES, unique=True)

        for modality in modalities:
            # We can't call the create method of the factory as
            # django_get_or_create would not be respected then
            self.modalities.add(ModalityFactory(code=modality))  # type: ignore


class QuestionFactory(BaseDjangoModelFactory[Question]):
    class Meta:
        model = Question

    job = factory.SubFactory("radis.rag.factories.RagJobFactory")
    question = factory.Faker("sentence", nb_words=10)
    accepted_answer = factory.Faker("random_element", elements=[a[0] for a in Answer.choices])


class RagTaskFactory(BaseDjangoModelFactory[RagTask]):
    class Meta:
        model = RagTask

    job = factory.SubFactory("radis.rag.factories.RagJobFactory")


class RagInstanceFactory(BaseDjangoModelFactory[RagInstance]):
    class Meta:
        model = RagInstance

    task = factory.SubFactory("radis.rag.factories.RagTaskFactory")

    @factory.post_generation
    def reports(self, create, extracted, **kwargs):
        if not create:
            return

        self = cast(RagInstance, self)

        if extracted:
            for report in extracted:
                self.reports.add(report)
        else:
            from radis.reports.factories import ReportFactory

            self.reports.add(*[ReportFactory() for _ in range(3)])
