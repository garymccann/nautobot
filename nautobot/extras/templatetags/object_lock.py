"""Assignment template tags exposing per-object Object Lock state to detail templates.

Each call performs one query, delegated to ``lock_state_for_objects``.
"""

from django import template
from django_jinja import library

from nautobot.extras.object_lock_ui import LOCK_PROTECTION_BLURB, lock_state_for_objects

register = template.Library()


@library.global_function(name="object_lock_state")
@register.simple_tag
def object_lock_state(obj):
    """Return the aggregated :class:`LockState` for ``obj``, or ``None`` if it has no active lock.

    Intended for assignment use in templates, e.g.::

        {% load object_lock %}
        {% object_lock_state object as object_lock %}
        {% if object_lock %} ... {% endif %}

    Returns ``None`` for a missing object or one lacking a primary key, so the template's ``{% if %}``
    guard renders the unlocked branch.

    Args:
        obj: A model instance, or ``None``.

    Returns:
        LockState | None: The lock state when the object currently has one or more active locks, else
        ``None``.
    """
    if obj is None or not hasattr(obj, "pk") or obj.pk is None:
        return None
    return lock_state_for_objects([obj]).get(obj.pk)


@library.global_function(name="object_lock_blurb")
@register.simple_tag
def object_lock_blurb():
    """Return the calibrated Object Lock protection blurb for use in template copy.

    Returns:
        str: ``LOCK_PROTECTION_BLURB`` (e.g. "protected against accidental deletion/edits ...").
    """
    return LOCK_PROTECTION_BLURB
