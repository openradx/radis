import json
from os import environ
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandParser
from faker import Faker

from radis.accounts.factories import AdminUserFactory, GroupFactory, UserFactory
from radis.accounts.models import User
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.reports.site import report_event_handlers
from radis.token_authentication.factories import TokenFactory
from radis.token_authentication.models import FRACTION_LENGTH
from radis.token_authentication.utils.crypto import hash_token

USER_COUNT = 20
GROUP_COUNT = 3
PACS_ITEMS = [
    {"pacs_aet": "gepacs", "pacs_name": "GE PACS"},
    {"pacs_aet": "synapse", "pacs_name": "Synapse"},
]
MODALITIES = ["CT", "MR", "PET", "CR", "US"]


fake = Faker()


def feed_report(body: str):
    report = ReportFactory.create(body=body)
    groups = fake.random_elements(elements=list(Group.objects.all()), unique=True)
    report.groups.set(groups)
    for handler in report_event_handlers:
        handler("created", report)


def feed_reports():
    samples_path = Path(settings.BASE_DIR / "samples" / "reports.json")
    with open(samples_path, "r") as f:
        reports = json.load(f)

    for report in reports:
        feed_report(report)


def create_admin() -> User:
    if "ADMIN_USERNAME" not in environ or "ADMIN_PASSWORD" not in environ:
        print("Cave! No admin credentials found in environment. Using default ones.")

    admin = AdminUserFactory.create(
        username=environ.get("ADMIN_USERNAME", "admin"),
        first_name=environ.get("ADMIN_FIRST_NAME", "Wilhelm"),
        last_name=environ.get("ADMIN_LAST_NAME", "Röntgen"),
        email=environ.get("ADMIN_EMAIL", "wilhelm.roentgen@example.org"),
        password=environ.get("ADMIN_PASSWORD", "mysecret"),
    )

    if "ADMIN_AUTH_TOKEN" not in environ:
        print("No admin auth token in environment. Skipping auth token creation.")
    else:
        auth_token = environ["ADMIN_AUTH_TOKEN"]
        TokenFactory.create(
            token_hashed=hash_token(auth_token),
            fraction=auth_token[:FRACTION_LENGTH],
            owner=admin,
            expires=None,
        )

    return admin


def create_users() -> list[User]:
    admin = create_admin()

    users = [admin]

    urgent_permissions = Permission.objects.filter(
        codename="can_process_urgently",
    )
    unpseudonymized_permissions = Permission.objects.filter(
        codename="can_transfer_unpseudonymized",
    )

    user_count = USER_COUNT - 1  # -1 for admin
    for i in range(user_count):
        user = UserFactory.create()

        if i > 0:
            user.user_permissions.add(*urgent_permissions)
            user.user_permissions.add(*unpseudonymized_permissions)

        users.append(user)

    return users


def create_groups(users: list[User]) -> list[Group]:
    groups: list[Group] = []

    for _ in range(GROUP_COUNT):
        group = GroupFactory.create()
        groups.append(group)

    for user in users:
        group: Group = fake.random_element(elements=groups)
        user.groups.add(group)
        if not user.active_group:
            user.change_active_group(group)

    return groups


class Command(BaseCommand):
    help = "Populates the database with example data."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

        parser.add_argument(
            "--skip-reports",
            action="store_true",
            help="Skip populating the database with example reports.",
        )

    def handle(self, *args, **options):
        if User.objects.count() > 0:
            print("Development database already populated. Skipping.")
        else:
            print("Populating development database with test data.")
            users = create_users()
            create_groups(users)

        if not options["skip_reports"]:
            if Report.objects.first():
                print("Reports already populated. Skipping.")
            else:
                print("Populating database with example reports.")
                feed_reports()
