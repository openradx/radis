from typing import Any, cast

from adit_radis_shared.accounts.models import User
from django.template import Library

from radis.reports.models import Report

from ..models import Note

register = Library()


@register.simple_tag(takes_context=True)
def note_available(context: dict[str, Any], report: Report):
    user = cast(User, context["request"].user)
    note = Note.objects.filter(owner=user, report_id=report.pk).first()
    return note is not None
