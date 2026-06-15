"""Targeted unit tests closing Object Lock coverage gaps (Bucket 1 + 2 of the coverage review).

These are real behavioural tests — template tags, glyph/summary helpers, filter methods, the sweep
Job wrapper, manager/diff edge paths, and m2m-enforcement early returns — each annotated with the
source line(s) it exercises. Defensive/unreachable guards (Bucket 3) are intentionally not covered.
"""

from datetime import timedelta
import uuid

from django import forms as dj_forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings, RequestFactory
from django.utils import timezone

from nautobot.core.testing import APITestCase, create_job_result_and_run_job, TestCase
from nautobot.dcim.filters import ManufacturerFilterSet
from nautobot.dcim.models import Location, LocationType, Manufacturer, Platform
from nautobot.extras import views
from nautobot.extras.api.object_locks import _batch_active_lock_claims
from nautobot.extras.choices import CustomFieldTypeChoices, JobResultStatusChoices
from nautobot.extras.context_managers import ORMChangeContext, web_request_context
from nautobot.extras.forms.forms import LockedFieldsFormMixin
from nautobot.extras.jobs_object_lock_sweep import purge_expired_and_orphaned_locks
from nautobot.extras.locking import (
    _prior_data_from_snapshot,
    _serialize_field_value,
    ALL_FIELDS_FROZEN,
    enforce_m2m_change,
    get_changed_fields,
    invalidate_gate_cache,
)
from nautobot.extras.models import CustomField, ObjectLock, ObjectLockBypassAudit, Status, Tag
from nautobot.extras.models.object_locks import ObjectLockGeneration, validate_locked_field_names
from nautobot.extras.object_lock_ui import (
    _glyph_token,
    LOCK_PROTECTION_BLURB,
    LockState,
    render_lock_glyph,
    summarize_modes,
)
from nautobot.extras.signals import change_context_state
from nautobot.extras.templatetags.object_lock import object_lock_blurb, object_lock_state

User = get_user_model()


class ObjectLockTemplateTagTestCase(TestCase):
    """templatetags/object_lock.py — both assignment tags (the module is otherwise only loaded by Selenium)."""

    def test_object_lock_state_tag(self):
        m = Manufacturer.objects.create(name="TplTag Locked")
        ObjectLock.objects.create(
            content_type=ContentType.objects.get_for_model(Manufacturer),
            object_id=m.pk,
            prevent_delete=True,
            source_key="tpl",
        )
        state = object_lock_state(m)
        self.assertIsNotNone(state)
        self.assertTrue(state.locked_for_delete)
        # Unlocked object, None, and a pk-less instance each resolve to None (the unlocked branch).
        self.assertIsNone(object_lock_state(Manufacturer.objects.create(name="TplTag Unlocked")))
        self.assertIsNone(object_lock_state(None))
        self.assertIsNone(object_lock_state(Manufacturer(name="TplTag NoPk")))

    def test_object_lock_blurb_tag(self):
        self.assertEqual(object_lock_blurb(), LOCK_PROTECTION_BLURB)


class ObjectLockGlyphHelperTestCase(TestCase):
    """object_lock_ui.py — glyph token, mode summary, and the metadata-rich tooltip."""

    def test_glyph_token_and_summary_per_mode(self):
        update_only = LockState(is_locked=True, locked_for_update=True, active_lock_count=1)
        self.assertEqual(_glyph_token(update_only), "update")
        self.assertEqual(summarize_modes(update_only), "Update-locked")
        unlocked = LockState()
        self.assertIsNone(_glyph_token(unlocked))
        self.assertEqual(summarize_modes(unlocked), "")

    def test_glyph_tooltip_includes_expiry_and_sources(self):
        state = LockState(
            is_locked=True,
            locked_for_delete=True,
            active_lock_count=2,
            earliest_expiry=timezone.now() + timedelta(days=1),
            source_keys=["alpha", "beta"],
        )
        html = str(render_lock_glyph(state, include_metadata=True))
        self.assertIn("earliest expiry", html)
        self.assertIn("sources:", html)


class ObjectLockBypassAuditAdminTestCase(TestCase):
    """admin.py — the bypass-audit admin is fully read-only."""

    def test_bypass_audit_admin_read_only(self):
        model_admin = admin.site._registry[ObjectLockBypassAudit]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))


class ObjectLockDunderStrTestCase(TestCase):
    """models/object_locks.py — __str__ for the audit row and the generation token."""

    def test_bypass_audit_str(self):
        m = Manufacturer.objects.create(name="Audit Str Mfg")
        audit = ObjectLockBypassAudit.objects.create(
            content_type=ContentType.objects.get_for_model(Manufacturer), object_id=m.pk
        )
        self.assertIn("Bypass by", str(audit))

    def test_generation_token_str(self):
        # The migration-seeded pk=1 row is cleared by the runner's generate_test_data --flush, so create
        # one explicitly rather than rely on the seed.
        gen = ObjectLockGeneration.objects.create(token=7)
        self.assertEqual(str(gen), "7")


class ObjectLockManagerEdgeTestCase(TestCase):
    """models/object_locks.py — indefinite TTL, locked() auto source_key, empty-field validation."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ol-cov-mgr")

    @override_settings(OBJECT_LOCK_DEFAULT_TTL=None)
    def test_lock_with_ttl_none_is_indefinite(self):
        m = Manufacturer.objects.create(name="TTL None Mfg")
        lock = ObjectLock.objects.lock(m, requesting_user=self.user)
        self.assertIsNone(lock.expires)

    def test_locked_context_manager_auto_source_key(self):
        m = Manufacturer.objects.create(name="Auto Key Mfg")
        with ObjectLock.objects.locked(m, requesting_user=self.user):
            self.assertEqual(ObjectLock.objects.for_object(m).count(), 1)
        self.assertEqual(ObjectLock.objects.for_object(m).count(), 0)

    def test_validate_empty_field_names_returns_empty(self):
        self.assertEqual(validate_locked_field_names(Manufacturer, []), [])
        self.assertEqual(validate_locked_field_names(Manufacturer, None), [])


class ObjectLockDiffEdgeTestCase(TestCase):
    """locking.py — get_changed_fields / serialize / snapshot edge branches + ALL_FIELDS_FROZEN sentinel."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ol-cov-diff")
        cls.ct = ContentType.objects.get_for_model(Manufacturer)

    def test_all_fields_frozen_sentinel(self):
        self.assertEqual(repr(ALL_FIELDS_FROZEN), "ALL_FIELDS_FROZEN")
        self.assertIn("anything", ALL_FIELDS_FROZEN)  # __contains__ is always True

    def test_empty_candidate_fields_returns_empty(self):
        m = Manufacturer.objects.create(name="Diff Empty")
        self.assertEqual(get_changed_fields(m, set()), set())

    def test_serialize_none_fk_value_returns_none(self):
        p = Platform.objects.create(name="Diff NoneFK")  # manufacturer FK is null
        self.assertIsNone(_serialize_field_value(p, "manufacturer"))

    def test_snapshot_missing_field_fails_closed(self):
        m = Manufacturer.objects.create(name="Diff SnapMiss", description="orig")
        ctx = ORMChangeContext(user=self.user)
        ctx.pre_object_data = {str(m.pk): {"name": "Diff SnapMiss"}}  # 'description' absent from snapshot
        token = change_context_state.set(ctx)
        try:
            m.description = "new"
            self.assertEqual(get_changed_fields(m, {"description"}), {"description"})
        finally:
            change_context_state.reset(token)

    def test_prior_data_none_when_snapshot_empty(self):
        m = Manufacturer.objects.create(name="Diff EmptySnap")
        ctx = ORMChangeContext(user=self.user)
        ctx.pre_object_data = {}  # context present but no snapshot rows
        token = change_context_state.set(ctx)
        try:
            self.assertIsNone(_prior_data_from_snapshot(m))
        finally:
            change_context_state.reset(token)

    def test_custom_field_changed_via_db_path(self):
        cf = CustomField.objects.create(type=CustomFieldTypeChoices.TYPE_TEXT, key="cov_db_cf", label="Cov DB CF")
        cf.content_types.set([self.ct])
        m = Manufacturer.objects.create(name="Diff CfDb")
        m._custom_field_data = {"cov_db_cf": "orig"}
        m.save()
        m.refresh_from_db()
        m._custom_field_data["cov_db_cf"] = "changed"
        # No change context -> snapshot None -> custom-field prior is read from the DB row.
        self.assertEqual(get_changed_fields(m, {"cov_db_cf"}), {"cov_db_cf"})

    def test_custom_field_fail_closed_when_not_in_db(self):
        cf = CustomField.objects.create(type=CustomFieldTypeChoices.TYPE_TEXT, key="cov_fc_cf", label="Cov FC CF")
        cf.content_types.set([self.ct])
        m = Manufacturer(name="Diff CfUnsaved")  # not saved
        m._custom_field_data = {"cov_fc_cf": "x"}
        # No snapshot and not in the DB -> prior unknowable -> fail closed.
        self.assertEqual(get_changed_fields(m, {"cov_fc_cf"}), {"cov_fc_cf"})


class ObjectLockFilterMethodTestCase(APITestCase):
    """filters.py — locked_for_delete / locked_for_update filters + the value-None passthrough."""

    def setUp(self):
        super().setUp()
        self.delete_locked = Manufacturer.objects.create(name="FM Delete Locked")
        self.update_locked = Manufacturer.objects.create(name="FM Update Locked")
        self.unlocked = Manufacturer.objects.create(name="FM Unlocked")
        ObjectLock.objects.lock(self.delete_locked, prevent_delete=True, requesting_user=self.user)
        ObjectLock.objects.lock(
            self.update_locked, prevent_update=True, prevent_delete=False, requesting_user=self.user
        )
        self.add_permissions("dcim.view_manufacturer")

    def test_filter_locked_for_delete(self):
        resp = self.client.get("/api/dcim/manufacturers/?locked_for_delete=true", **self.header)
        names = {row["name"] for row in resp.data["results"]}
        self.assertIn("FM Delete Locked", names)
        self.assertNotIn("FM Update Locked", names)
        self.assertNotIn("FM Unlocked", names)

    def test_filter_locked_for_update(self):
        resp = self.client.get("/api/dcim/manufacturers/?locked_for_update=true", **self.header)
        names = {row["name"] for row in resp.data["results"]}
        self.assertIn("FM Update Locked", names)
        self.assertNotIn("FM Delete Locked", names)

    def test_lock_filter_none_passthrough(self):
        filterset = ManufacturerFilterSet()
        queryset = Manufacturer.objects.all()
        self.assertEqual(list(filterset._apply_lock_filter(queryset, None)), list(queryset))


class ObjectLockApiEdgeTestCase(APITestCase):
    """api/object_locks.py — empty-batch helper + past-expiry rejection on the lock action."""

    def setUp(self):
        super().setUp()
        self.mfg = Manufacturer.objects.create(name="Api Edge Mfg")

    def test_batch_active_lock_claims_empty(self):
        self.assertEqual(_batch_active_lock_claims([]), {})

    def test_lock_action_rejects_past_expires(self):
        self.add_permissions("extras.add_objectlock", "dcim.view_manufacturer")
        url = f"/api/dcim/manufacturers/{self.mfg.pk}/lock/"
        past = (timezone.now() - timedelta(days=1)).isoformat()
        resp = self.client.post(url, {"prevent_delete": True, "expires": past}, format="json", **self.header)
        self.assertHttpStatus(resp, 400)


class ObjectLockSweepJobTestCase(TestCase):
    """jobs_object_lock_sweep.py — the Job run() wrapper and the uninstalled-model orphan purge."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ol-cov-sweep", is_superuser=True)

    def test_sweep_job_run(self):
        m = Manufacturer.objects.create(name="Sweep Job Mfg")
        lock = ObjectLock.objects.lock(m, source_key="covsweep", requesting_user=self.user)
        ObjectLock.objects.filter(pk=lock.pk).update(expires=timezone.now() - timedelta(hours=1))
        job_result = create_job_result_and_run_job("nautobot.extras.jobs_object_lock_sweep", "ObjectLockSweep")
        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_SUCCESS)
        self.assertFalse(ObjectLock.objects.filter(pk=lock.pk).exists())

    def test_sweep_purges_uninstalled_model_locks(self):
        ghost_ct = ContentType.objects.create(app_label="ghostapp_cov", model="ghostmodel")
        ObjectLock.objects.create(
            content_type=ghost_ct, object_id=uuid.uuid4(), prevent_delete=True, source_key="ghost"
        )
        result = purge_expired_and_orphaned_locks()
        self.assertFalse(ObjectLock.objects.filter(content_type=ghost_ct).exists())
        self.assertGreaterEqual(result["orphaned"], 1)


class ObjectLockFormMixinUnboundTestCase(TestCase):
    """forms/forms.py — an unbound LockedFieldsFormMixin form resolves an empty frozen-field set."""

    def test_unbound_form_has_no_frozen_fields(self):
        class _SampleForm(LockedFieldsFormMixin, dj_forms.Form):
            name = dj_forms.CharField(required=False)

        form = _SampleForm()  # no frozen_fields kwarg, no instance -> _frozen_fields_for_instance() -> set()
        self.assertEqual(form._frozen_fields, set())
        self.assertFalse(form.fields["name"].disabled)


class ObjectLockBulkReturnUrlTestCase(TestCase):
    """views.py — ObjectLockBulkActionView._safe_return_url accepts a safe local URL."""

    def test_safe_return_url_accepts_local(self):
        view = views.ObjectLockBulkActionView()
        # RequestFactory (unlike the test Client) doesn't auto-allow "testserver"; use an allowed host.
        request = RequestFactory().post("/", {"return_url": "/dcim/manufacturers/"}, SERVER_NAME="nautobot.example.com")
        self.assertEqual(view._safe_return_url(request), "/dcim/manufacturers/")


class ObjectLockM2MEarlyReturnTestCase(TestCase):
    """locking.py + signals.py — m2m-enforcement early returns and the kill switch."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_user(username="ol-cov-m2m", is_superuser=True)
        location_type = LocationType.objects.create(name="Region-Cov")
        location_type.content_types.add(ContentType.objects.get_for_model(Location))
        cls.status = Status.objects.get_for_model(Location).first()
        cls.location = Location.objects.create(name="LocCov", location_type=location_type, status=cls.status)
        cls.tag = Tag.objects.create(name="TagCov")
        cls.tag.content_types.add(ContentType.objects.get_for_model(Location))

    def setUp(self):
        invalidate_gate_cache()

    def test_enforce_m2m_non_pre_action_returns(self):
        enforce_m2m_change(self.location, "tags", "post_add")  # non-pre action -> early return, no raise

    def test_enforce_m2m_not_in_database_returns(self):
        enforce_m2m_change(Location(name="ghost"), "tags", "pre_add")  # unsaved -> early return, no raise

    def test_enforce_m2m_gate_miss_returns(self):
        enforce_m2m_change(self.location, "tags", "pre_add")  # no lock on this type -> gate miss, no raise

    @override_settings(OBJECT_LOCK_ENFORCED=False)
    def test_m2m_kill_switch_allows_frozen(self):
        with web_request_context(self.superuser):
            ObjectLock.objects.lock(
                self.location,
                prevent_update=True,
                locked_fields=["tags"],
                requesting_user=self.superuser,
                source_key="cov-m2m-frozen",
            )
        invalidate_gate_cache()
        with web_request_context(self.superuser):
            self.location.tags.add(self.tag)  # kill switch disables enforcement
        self.assertIn(self.tag, self.location.tags.all())
