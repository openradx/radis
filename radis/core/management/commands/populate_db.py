import json
from os import environ
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from faker import Faker

from radis.accounts.factories import AdminUserFactory, GroupFactory, UserFactory
from radis.accounts.models import User
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.token_authentication.factories import TokenFactory
from radis.token_authentication.models import FRACTION_LENGTH
from radis.token_authentication.utils.crypto import hash_token
from radis.vespa.utils.document_utils import create_documents

USER_COUNT = 20
GROUP_COUNT = 3


fake = Faker()


def create_report(body: str, language: Literal["en", "de"]):
    report = ReportFactory.create(language=language, body=body)
    groups = fake.random_elements(elements=list(Group.objects.all()), unique=True)
    report.groups.set(groups)
    return report


def feed_reports(language: Literal["en", "de"]):
    if language == "en":
        sample_file = "reports_en.json"
    elif language == "de":
        sample_file = "reports_de.json"
    else:
        raise ValueError(f"Language {language} is not supported.")

    samples_path = Path(settings.BASE_DIR / "samples" / sample_file)
    with open(samples_path, "r") as f:
        report_bodies = json.load(f)

    reports: list[Report] = []
    for report_body in report_bodies:
        reports.append(create_report(report_body, language))

    transaction.on_commit(lambda: create_documents([report.id for report in reports]))


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
        parser.add_argument(
            "--report-language",
            default="en",
            help="Which report language to use (en or de).",
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
                feed_reports(options["report_language"])
