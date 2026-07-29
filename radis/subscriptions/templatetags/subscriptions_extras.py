from collections.abc import Mapping
from typing import Any

from django import template

register = template.Library()


@register.filter
def get_item(mapping: Mapping[str, Any] | None, key: Any) -> Any:
    """
    Safely retrieve a value from a mapping for template usage.
    """
    if mapping is None:
        return None
    try:
        return mapping.get(str(key))
    except AttributeError:
        return None
