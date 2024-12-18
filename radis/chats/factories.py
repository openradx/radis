import factory

from .models import Grammar


class BaseDjangoModelFactory[T](factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class GrammarFactory(BaseDjangoModelFactory[Grammar]):
    class Meta:
        model = Grammar

    name = factory.Faker("word")
    human_readable_name = factory.Faker("word")
    grammar = factory.Faker("sentence")
    llm_instruction = factory.Faker("sentence")
    is_default = False
