from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group

from radis.extractions.factories import OutputFieldFactory
from radis.extractions.models import OutputType
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.subscriptions.factories import FilterQuestionFactory, SubscriptionFactory
from radis.subscriptions.models import FilterQuestion, SubscriptionJob, SubscriptionTask


def create_subscription_task():
    language = LanguageFactory.create(code="en")

    user = UserFactory(is_active=True)
    group = GroupFactory()
    add_user_to_group(user, group)
    user.active_group = group
    user.save()

    subscription = SubscriptionFactory.create(owner=user, group=group, language=language)

    filter_question = FilterQuestionFactory.create(
        subscription=subscription, expected_answer=FilterQuestion.ExpectedAnswer.YES
    )
    output_field = OutputFieldFactory.create(
        subscription=subscription,
        job=None,
        output_type=OutputType.TEXT,
    )

    job = SubscriptionJob.objects.create(
        subscription=subscription,
        owner=user,
        owner_id=user.id,
        status=SubscriptionJob.Status.PENDING,
    )
    task = SubscriptionTask.objects.create(job=job, status=SubscriptionTask.Status.PENDING)

    report = ReportFactory.create(language=language, body="Pneumothorax observed.")
    report.groups.add(group)
    task.reports.add(report)

    return task, filter_question, output_field, report
