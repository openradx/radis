import factory
from adit_radis_shared.accounts.factories import UserFactory
from adit_radis_shared.common.factories import BaseDjangoModelFactory
from faker import Faker

from radis.reports.factories import ReportFactory

from .models import Note

fake = Faker()


class NoteFactory(BaseDjangoModelFactory[Note]):
    class Meta:
        model = Note

    owner = factory.SubFactory(UserFactory)
    report = factory.SubFactory(ReportFactory)
    text = factory.Faker("paragraph", nb_sentences=5)
