from typing import Any, cast

from adit_radis_shared.accounts.models import User
from django.db.models import QuerySet
from django.template import Library

from radis.reports.models import Report

from ..models import Collection

register = Library()


@register.simple_tag(takes_context=True)
def collections_count(context: dict[str, Any], report: Report):
    user = cast(User, context["request"].user)
    collections = cast(QuerySet[Collection], getattr(report, "collections"))
    return collections.filter(owner=user).count()
