"""Car C3 (0047 §7 D21): identity gate for deterministic-slug page types.

Re-implements the ``gate_mode="identity"`` dispatch that was removed with
repo_wiki's decommission (#33/ADR-0162). For page types with deterministic
slugs (``adr``, ``task_list``, agent-prompt library), a page's identity IS its
slug — content similarity is irrelevant (two ADR pages are structurally
near-identical by design), so the drainer gate is a pass-through and the
upsert=False slug-collision check at ``WikiStore.add`` handles real
collisions.

Behaviour under test:
- ``page_type="adr"`` wiki_add with near-duplicate content passes the gate
  WITHOUT ``force=True``.
- ``page_type="task_list"`` wiki_add with near-duplicate content passes the
  gate WITHOUT ``force=True`` or ``replace_slug``.
- ``page_type="reference"`` (and ``page_type=None``) wiki_add with
  near-duplicate content is STILL rejected by the similarity gate (default
  path unchanged).
- ``get_policy("adr").gate_mode == "identity"`` and
  ``get_policy("task_list").gate_mode == "identity"`` and
  ``get_policy("agent_pattern").gate_mode == "identity"``.
- ``get_policy(None).gate_mode == "similarity"`` and
  ``get_policy("reference").gate_mode == "similarity"`` (default unchanged).

Drainer-level tests call ``_sim_gate_for_drainer()`` directly via the helper
``_drainer_gate(payload)`` so they exercise the policy dispatch in isolation
without requiring the full wiki_add pipeline.
"""

from __future__ import annotations

from yadgar._shared.wiki.policy import DEFAULT_POLICY, get_policy

# ── Policy-registry unit tests (pure, no DB) ─────────────────────────────────


class TestPolicyRegistryIdentityGate:
    """POLICY_BY_TYPE entries for adr / task_list / agent-prompt library."""

    def test_get_policy_adr_gate_mode_is_identity(self):
        policy = get_policy("adr")
        assert policy.gate_mode == "identity", (
            f"page_type='adr' must use the identity gate (D21), got {policy.gate_mode!r}"
        )

    def test_get_policy_task_list_gate_mode_is_identity(self):
        policy = get_policy("task_list")
        assert policy.gate_mode == "identity", (
            f"page_type='task_list' must use the identity gate (D21), got {policy.gate_mode!r}"
        )

    def test_get_policy_agent_pattern_gate_mode_is_identity(self):
        """Agent-prompt library types share _AGENT_LIBRARY_POLICY (ADR-0209)."""
        policy = get_policy("agent_pattern")
        assert policy.gate_mode == "identity", (
            f"page_type='agent_pattern' must use the identity gate, got {policy.gate_mode!r}"
        )

    def test_get_policy_agent_discipline_gate_mode_is_identity(self):
        policy = get_policy("agent_discipline")
        assert policy.gate_mode == "identity"

    def test_get_policy_agent_prompt_legacy_gate_mode_is_identity(self):
        """Pre-ADR-0209 legacy type — covered by _AGENT_LIBRARY_POLICY."""
        policy = get_policy("agent_prompt")
        assert policy.gate_mode == "identity"

    def test_get_policy_agent_index_unchanged(self):
        """TOC index stays on similarity gate (only gate_mode flipped for library types).

        Car C1 split agent_index onto _AGENT_INDEX_POLICY; C3 only flips the
        gate_mode of the shared library policy. The TOC was NEVER on the
        library policy so its gate_mode was 'similarity' pre-C3 and stays that
        way post-C3.
        """
        policy = get_policy("agent_index")
        assert policy.gate_mode == "similarity"

    def test_get_policy_reference_default_unchanged(self):
        """Free-form page types still run the similarity gate."""
        policy = get_policy("reference")
        assert policy.gate_mode == "similarity"
        assert policy is DEFAULT_POLICY or policy.gate_mode == "similarity"

    def test_get_policy_none_default_unchanged(self):
        """Null page_type falls through to DEFAULT_POLICY (similarity gate)."""
        policy = get_policy(None)
        assert policy.gate_mode == "similarity"
        assert policy is DEFAULT_POLICY


# ── Drainer dispatch unit tests (in-process QueueDrainer, no DB seed) ────────


def _drainer_gate(payload: dict) -> dict | None:
    """Call ``_sim_gate_for_drainer()`` on a transient drainer.

    Mirrors the helper in test_wiki_similarity_gate.py — builds a QueueDrainer
    against a temp dir, invokes the dispatch directly. Used for unit tests of
    the policy dispatch (no DB seed required because the identity gate is a
    pass-through and the similarity gate's bypass returns ``None`` when the
    store is unavailable).
    """
    import tempfile

    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    with tempfile.TemporaryDirectory() as tmp:
        fq = FileQueue(tmp)
        drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
        return drainer._sim_gate_for_drainer(payload)


class TestSimGateDispatcherIdentityMode:
    """``_sim_gate_for_drainer`` reads ``get_policy(page_type).gate_mode``."""

    def test_adr_page_type_dispatches_to_identity_gate(self):
        """page_type='adr' → identity gate → returns None (pass-through).

        With NO store seeded, the identity gate must NOT call the similarity
        path. The pass-through returns ``None`` so the write proceeds.
        """
        payload = {
            "title": "My ADR",
            "content": "near-duplicate content",
            "slug": "my-project-adr-0001",
            "page_type": "adr",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, (
            f"identity gate must pass-through 'adr' page_type. Got: {rejection}"
        )

    def test_task_list_page_type_dispatches_to_identity_gate(self):
        payload = {
            "title": "Task List",
            "content": "near-duplicate content",
            "slug": "my-project-task-list",
            "page_type": "task_list",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, (
            f"identity gate must pass-through 'task_list' page_type. Got: {rejection}"
        )

    def test_agent_pattern_page_type_dispatches_to_identity_gate(self):
        payload = {
            "title": "Agent Pattern",
            "content": "structural scaffolding content",
            "slug": "agent-pattern-yadgar",
            "page_type": "agent_pattern",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, (
            f"identity gate must pass-through 'agent_pattern' page_type. Got: {rejection}"
        )

    def test_reference_page_type_falls_through_to_similarity(self):
        """page_type='reference' (free-form) still runs the similarity gate.

        No store seeded → similarity gate's None-guard returns None too (the
        gate is bypassed when ``_st._wiki`` is None). The contract under test
        here is the DISPATCH: the policy read must NOT raise, and the dispatch
        must NOT take the identity path for a non-identity page_type.
        """
        payload = {
            "title": "Reference Doc",
            "content": "any content",
            "slug": "ref-doc",
            "page_type": "reference",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        # No assertion on rejection value (the similarity gate's None-guard
        # would also return None with no store). The point is the dispatch
        # doesn't raise and doesn't take the identity path — it must call
        # _similarity_gate_for_drainer, not _identity_gate_for_drainer.
        rejection = _drainer_gate(payload)
        # Both paths can legitimately return None here. What we MUST NOT see:
        #   - an exception (dispatch bug)
        #   - a rejection dict keyed on the wrong policy (identity gate
        #     does not produce one, similarity gate can with a seeded store)
        # The presence of None is the contract.
        assert rejection is None or (
            isinstance(rejection, dict) and rejection.get("reason") == "duplicate_detected"
        ), f"unexpected dispatch result: {rejection}"

    def test_none_page_type_falls_through_to_similarity(self):
        payload = {
            "title": "Untyped Doc",
            "content": "any content",
            "slug": "untyped-doc",
            "page_type": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None or (
            isinstance(rejection, dict) and rejection.get("reason") == "duplicate_detected"
        ), f"unexpected dispatch result: {rejection}"

    def test_bypass_conditions_still_apply_under_identity_mode(self):
        """force=True / replace_slug / append bypass BOTH gate modes.

        The bypass checks at dlq.py:219-224 short-circuit BEFORE the policy
        dispatch, so an 'adr' payload with force=True also returns None — the
        bypass semantics are unchanged for identity-gated types.
        """
        for bypass_key, bypass_val in [
            ("force", True),
            ("replace_slug", "existing-slug"),
            ("append", True),
        ]:
            payload = {
                "title": "ADR",
                "content": "any content",
                "slug": "my-project-adr-0001",
                "page_type": "adr",
                "force": False,
                "replace_slug": None,
                "append": False,
                bypass_key: bypass_val,
            }
            rejection = _drainer_gate(payload)
            assert rejection is None, (
                f"bypass {bypass_key}={bypass_val!r} must short-circuit BEFORE the policy "
                f"dispatch. Got: {rejection}"
            )


class TestIdentityGateForDrainerContract:
    """``_identity_gate_for_drainer`` is a documented pass-through."""

    def test_identity_gate_for_drainer_returns_none_for_adr(self):
        """Direct call to _identity_gate_for_drainer returns None."""
        import tempfile

        from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
            assert hasattr(drainer, "_identity_gate_for_drainer"), (
                "_identity_gate_for_drainer must be defined on the drainer mixin"
            )
            payload = {
                "title": "My ADR",
                "content": "near-duplicate content",
                "slug": "my-project-adr-0001",
                "page_type": "adr",
            }
            result = drainer._identity_gate_for_drainer(payload)
            assert result is None, (
                f"_identity_gate_for_drainer must return None (pass-through), got {result!r}"
            )

    def test_identity_gate_for_drainer_returns_none_for_task_list(self):
        import tempfile

        from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
            payload = {
                "title": "Task List",
                "content": "structural task list content",
                "slug": "my-project-task-list",
                "page_type": "task_list",
            }
            assert drainer._identity_gate_for_drainer(payload) is None


# ── ADR canonical payload contract ──────────────────────────────────────────


class TestCanonicalAdrPayloadNoForce:
    """_canonical_adr_payload must NOT set ``force=True`` once 'adr' is identity-gated."""

    def test_canonical_adr_payload_does_not_set_force_true(self):
        """The deleted ``"force": True`` line at adr_render.py:160 stays deleted.

        Car C3 deletes it because ``page_type='adr'`` now dispatches to the
        identity gate, which is a pass-through — no bypass needed.
        """
        from yadgar.core.server.tools.adr_render import _canonical_adr_payload

        payload = _canonical_adr_payload(
            slug="my-project-adr-0001",
            content="ADR body content",
            category="decision",
            tags=["adr-status:accepted"],
            directory="/proj/example",
        )
        assert "force" not in payload or payload.get("force") is False, (
            f"_canonical_adr_payload must not set force=True (adr is identity-gated). Got: "
            f"{payload.get('force')!r}"
        )

    def test_canonical_adr_payload_still_has_required_keys(self):
        """Sanity: the rest of the payload is unchanged."""
        from yadgar.core.server.tools.adr_render import _canonical_adr_payload

        payload = _canonical_adr_payload(
            slug="my-project-adr-0001",
            content="ADR body content",
            category="decision",
            tags=["adr-status:accepted"],
            directory="/proj/example",
        )
        assert payload["page_type"] == "adr"
        assert payload["slug"] == "my-project-adr-0001"
        assert payload["title"] == "my-project-adr-0001"
        assert payload["wiki_schema_version"] == 2
        assert payload["append"] is False
        assert payload["replace_slug"] is None
        assert payload["directory_context"] == "/proj/example"


# ── Similarity gate regression: free-form pages still go through similarity ─


class TestSimilarityGateRegression:
    """page_type='reference' (free-form) still routes through the similarity gate.

    Regression guard for §5 acceptance: the identity gate must not swallow the
    default similarity path. Verified by monkeypatching
    ``_identity_gate_for_drainer`` and ``_similarity_gate_for_drainer`` and
    asserting which one gets called for each page_type. This avoids needing a
    seeded DB and the full wiki_add pipeline (which needs YADGAR_EMBED_URL for
    wait=True).
    """

    def _spy_drainer(self, monkeypatch):
        """Build a drainer that records which gate method is invoked."""
        import tempfile

        from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

        calls = {"identity": 0, "similarity": 0}

        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)

            original_identity = drainer._identity_gate_for_drainer
            original_similarity = drainer._similarity_gate_for_drainer

            def _identity_spy(payload):
                calls["identity"] += 1
                return original_identity(payload)

            def _similarity_spy(payload):
                calls["similarity"] += 1
                return original_similarity(payload)

            monkeypatch.setattr(drainer, "_identity_gate_for_drainer", _identity_spy)
            monkeypatch.setattr(drainer, "_similarity_gate_for_drainer", _similarity_spy)

            # Bypass (force/replace_slug/append) short-circuits before either gate,
            # so the spy is not exercised. Drive the dispatch only.
            payload = {
                "title": "Doc",
                "content": "any content",
                "slug": "doc-slug",
                "force": False,
                "replace_slug": None,
                "append": False,
            }
            return drainer, calls, payload

    def test_reference_page_type_routes_to_similarity_gate(self, monkeypatch):
        """Free-form page_type='reference' routes to _similarity_gate_for_drainer."""
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = "reference"
        drainer._sim_gate_for_drainer(payload)
        assert calls["similarity"] == 1, (
            f"reference page_type must dispatch to similarity gate. "
            f"identity={calls['identity']}, similarity={calls['similarity']}"
        )
        assert calls["identity"] == 0, "reference must NOT route to identity gate"

    def test_none_page_type_routes_to_similarity_gate(self, monkeypatch):
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = None
        drainer._sim_gate_for_drainer(payload)
        assert calls["similarity"] == 1
        assert calls["identity"] == 0

    def test_adr_page_type_routes_to_identity_gate(self, monkeypatch):
        """page_type='adr' routes to _identity_gate_for_drainer (not similarity)."""
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = "adr"
        drainer._sim_gate_for_drainer(payload)
        assert calls["identity"] == 1, (
            f"adr page_type must dispatch to identity gate. "
            f"identity={calls['identity']}, similarity={calls['similarity']}"
        )
        assert calls["similarity"] == 0, "adr must NOT route to similarity gate"

    def test_task_list_page_type_routes_to_identity_gate(self, monkeypatch):
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = "task_list"
        drainer._sim_gate_for_drainer(payload)
        assert calls["identity"] == 1
        assert calls["similarity"] == 0

    def test_agent_pattern_page_type_routes_to_identity_gate(self, monkeypatch):
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = "agent_pattern"
        drainer._sim_gate_for_drainer(payload)
        assert calls["identity"] == 1
        assert calls["similarity"] == 0

    def test_unknown_page_type_routes_to_similarity_gate(self, monkeypatch):
        """An unrecognised page_type falls through to DEFAULT_POLICY (similarity)."""
        drainer, calls, payload = self._spy_drainer(monkeypatch)
        payload["page_type"] = "totally_unknown_type"
        drainer._sim_gate_for_drainer(payload)
        assert calls["similarity"] == 1
        assert calls["identity"] == 0
