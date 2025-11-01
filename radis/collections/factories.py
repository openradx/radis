import factory
from adit_radis_shared.accounts.factories import UserFactory
from faker import Faker

from .models import Collection

fake = Faker()


class BaseDjangoModelFactory[T](factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class CollectionFactory(BaseDjangoModelFactory[Collection]):
    class Meta:
        model = Collection

    name = factory.LazyAttribute(lambda obj: fake.sentence(nb_words=3)[:60])
    owner = factory.SubFactory(UserFactory)
