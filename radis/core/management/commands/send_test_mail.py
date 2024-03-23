from adit_radis_shared.common.management.base.send_test_mail import (
    SendTestMailCommand,
)


class Command(SendTestMailCommand):
    project_name = "RADIS"
