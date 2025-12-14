import factory
from faker import Faker

from adit_radis_shared.accounts.factories import UserFactory
from radis.reports.factories import ModalityFactory

from .models import (
    ExtractionInstance,
    ExtractionJob,
    ExtractionTask,
    OutputField,
    OutputType,
)

fake = Faker()

MODALITIES = ("CT", "MR", "DX", "PT", "US")


class BaseDjangoModelFactory[T](factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class ExtractionJobFactory(BaseDjangoModelFactory):
    class Meta:
        model = ExtractionJob

    owner = factory.SubFactory(UserFactory)
    title = factory.Faker("sentence", nb_words=3)
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

    @factory.post_generation
    def ensure_owner_in_group(self, create, extracted, **kwargs):
        if not create:
            return

        self.owner.groups.add(self.group)
        if self.owner.active_group_id is None:
            self.owner.active_group = self.group
            self.owner.save(update_fields=["active_group"])


class OutputFieldFactory(BaseDjangoModelFactory[OutputField]):
    class Meta:
        model = OutputField

    job = factory.SubFactory("radis.extractions.factories.ExtractionJobFactory")
    name = factory.Sequence(lambda n: f"output_field_{n}")
    description = factory.Faker("sentence", nb_words=10)
    output_type = factory.Faker("random_element", elements=[a[0] for a in OutputType.choices])
    selection_options = factory.LazyAttribute(
        lambda obj: ["Option 1", "Option 2"] if obj.output_type == OutputType.SELECTION else []
    )


class ExtractionTaskFactory(BaseDjangoModelFactory[ExtractionTask]):
    class Meta:
        model = ExtractionTask

    job = factory.SubFactory("radis.extractions.factories.ExtractionJobFactory")


class ExtractionInstanceFactory(BaseDjangoModelFactory[ExtractionInstance]):
    class Meta:
        model = ExtractionInstance

    task = factory.SubFactory("radis.extractions.factories.ExtractionTaskFactory")
    report = factory.SubFactory("radis.reports.factories.ReportFactory")
