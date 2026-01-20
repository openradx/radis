from typing import cast

import factory
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.accounts.models import User
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group
from django.contrib.auth.models import Group
from factory.declarations import SKIP
from faker import Faker

from radis.reports.factories import ModalityFactory

from .models import ExtractionInstance, ExtractionJob, ExtractionTask, OutputField, OutputType

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
    group = factory.SubFactory(GroupFactory)
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
    def ensure_owner_in_group(obj, create, extracted, **kwargs):
        owner = cast(User, obj.owner)
        group = cast(Group, obj.group)

        if not create:
            return

        add_user_to_group(owner, group)


class OutputFieldFactory(BaseDjangoModelFactory[OutputField]):
    class Meta:
        model = OutputField

    # Use factory.Maybe to conditionally create job only when subscription is None
    job = factory.Maybe(
        factory.SelfAttribute("subscription"),
        yes_declaration=SKIP,  # If subscription exists, skip job creation
        no_declaration=factory.SubFactory("radis.extractions.factories.ExtractionJobFactory"),  # type: ignore[arg-type]
    )
    subscription = None
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
