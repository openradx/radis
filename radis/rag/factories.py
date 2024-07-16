from typing import Generic, TypeVar, cast

import factory
from faker import Faker

from radis.reports.factories import ModalityFactory

from .models import Answer, Question, RagJob, RagReportInstance, RagTask

T = TypeVar("T")

fake = Faker()


class BaseDjangoModelFactory(Generic[T], factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


SearchProviders = ("OpenSearch", "Vespa", "Elasticsearch")
PatientSexes = ["", "M", "F"]


class RagJobFactory(BaseDjangoModelFactory):
    class Meta:
        model = RagJob

    title = factory.Faker("sentence", nb_words=3)
    provider = factory.Faker("random_element", elements=SearchProviders)
    group = factory.SubFactory("adit_radis_shared.accounts.factories.GroupFactory")
    query = factory.Faker("word")
    language = factory.SubFactory("radis.reports.factories.LanguageFactory")
    study_date_from = factory.Faker("date")
    study_date_till = factory.Faker("date")
    study_description = factory.Faker("sentence", nb_words=5)
    patient_sex = factory.Faker("random_element", elements=PatientSexes)
    age_from = factory.Faker("random_int", min=0, max=100)
    age_till = factory.Faker("random_int", min=0, max=100)

    @factory.post_generation
    def modalities(self, create, extracted, **kwargs):
        if not create:
            return

        self = cast(RagJob, self)

        if extracted:
            for modality in extracted:
                self.modalities.add(modality)
        else:
            modality = ModalityFactory()
            self.modalities.add(modality)


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


class RagReportInstanceFactory(BaseDjangoModelFactory[RagReportInstance]):
    class Meta:
        model = RagReportInstance

    task = factory.SubFactory("radis.rag.factories.RagTaskFactory")
    report = factory.SubFactory("radis.reports.factories.ReportFactory")
