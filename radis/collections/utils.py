from radis.accounts.models import User
from radis.core.models import Report


def get_report_collections_count(report: Report, owner: User) -> int:
    collections = getattr(report, "collections", None)
    assert collections
    return collections.filter(owner=owner).count()
