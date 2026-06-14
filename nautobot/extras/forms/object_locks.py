"""Forms for the minimal Object Lock management view."""

from django import forms
from django.contrib.auth import get_user_model

from nautobot.core.forms import APISelectMultiple, DynamicModelMultipleChoiceField
from nautobot.extras.forms.base import NautobotFilterForm
from nautobot.extras.models import ObjectLock


class ObjectLockFilterForm(NautobotFilterForm):
    """Filter form for the Object Lock list view.

    Exposes the high-value ObjectLockFilterSet fields so locks can be filtered by mode, source, and
    creator. Expiry-range filtering remains available on the filterset (e.g. ``?expires__gte=...`` /
    ``?expires__lte=...``) via the REST API and URL; it is not rendered as a sidebar field because
    ``NautobotFilterForm`` feeds multi-value data and a single datetime input cannot accept a list.
    """

    model = ObjectLock
    field_order = [
        "q",
        "prevent_delete",
        "prevent_update",
        "source_key",
        "created_by",
    ]
    q = forms.CharField(required=False, label="Search")
    prevent_delete = forms.NullBooleanField(required=False)
    prevent_update = forms.NullBooleanField(required=False)
    source_key = forms.CharField(required=False, label="Source")
    created_by = DynamicModelMultipleChoiceField(
        queryset=get_user_model().objects.all(),
        required=False,
        label="Created by",
        widget=APISelectMultiple(api_url="/api/users/users/"),
    )
