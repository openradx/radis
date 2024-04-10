from typing import Any, cast

from django.template import Library

from adit_radis_shared.accounts.models import User

from ..models import Report

register = Library()


@register.simple_tag(takes_context=True)
def can_view_report(context: dict[str, Any], report: Report) -> bool:
    user = cast(User, context["request"].user)
    active_group = user.active_group
    if not active_group:
        return False
    return report.groups.filter(pk=active_group.pk).exists()
