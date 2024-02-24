from typing import Any, cast

from django.template import Library

from radis.accounts.models import User
from radis.collections.utils import get_report_collections_count

from ..models import Report

register = Library()


@register.simple_tag(takes_context=True)
def collections_count(context: dict[str, Any], report: Report):
    user = cast(User, context["request"].user)
    return get_report_collections_count(report, user)
