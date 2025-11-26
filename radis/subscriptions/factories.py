import factory
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.factories import BaseDjangoModelFactory

from radis.reports.factories import LanguageFactory, ReportFactory

from .models import FilterQuestion, SubscribedItem, Subscription, SubscriptionJob, SubscriptionTask


class SubscriptionFactory(BaseDjangoModelFactory[Subscription]):
    class Meta:
        model = Subscription

    name = factory.Faker("word")
    owner = factory.SubFactory(UserFactory)
    group = factory.SubFactory(GroupFactory)
    provider = factory.Faker("company")
    patient_id = factory.Faker("numerify", text="##########")
    query = factory.Faker("sentence", nb_words=3)
    language = factory.SubFactory(LanguageFactory, code="en")
    study_description = factory.Faker("sentence", nb_words=4)
    patient_sex = factory.Faker("random_element", elements=["M", "F", ""])
    age_from = factory.Faker("random_int", min=0, max=80)
    age_till = factory.LazyAttribute(lambda obj: obj.age_from + 20)
    send_finished_mail = factory.Faker("boolean")


class FilterQuestionFactory(BaseDjangoModelFactory[FilterQuestion]):
    class Meta:
        model = FilterQuestion

    subscription = factory.SubFactory(SubscriptionFactory)
    question = factory.Faker("sentence", nb_words=6, variable_nb_words=True)


class SubscriptionJobFactory(BaseDjangoModelFactory[SubscriptionJob]):
    class Meta:
        model = SubscriptionJob

    subscription = factory.SubFactory(SubscriptionFactory)
    owner = factory.SelfAttribute("subscription.owner")


class SubscriptionTaskFactory(BaseDjangoModelFactory[SubscriptionTask]):
    class Meta:
        model = SubscriptionTask

    job = factory.SubFactory(SubscriptionJobFactory)


class SubscribedItemFactory(BaseDjangoModelFactory[SubscribedItem]):
    class Meta:
        model = SubscribedItem

    subscription = factory.SubFactory(SubscriptionFactory)
    job = factory.SubFactory(
        SubscriptionJobFactory, subscription=factory.SelfAttribute("..subscription")
    )
    report = factory.SubFactory(ReportFactory)
