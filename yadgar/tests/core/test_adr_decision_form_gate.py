"""Car B — the write-time gate on car-shaped ADR decisions.

A 272-ADR corpus audit (2026-08-28) found the live drift is NOT "memories filed
as ADRs" — every ADR authored since 2026-08-19 carries a substantive
rejected-alternatives list.  The failure is narrower: FORM drift, a per-car ADR
whose ``decision`` names a CAR, a TRAIN or a LEDGER ROW as its subject.  It
reads as a worklog even when every other field is excellent.

The rule the gate enforces (agreed with the user, 2026-08-28):

    An ADR's Decision states the RULE, not the work that produced it.  Context
    may cite the incident; the Decision may not name a car, a train, or a
    ledger row as its SUBJECT.

Precision is the whole design constraint: a false positive BLOCKS a legitimate
ADR write, which is strictly worse than missing one.  Every fixture below is
VERBATIM corpus text, and the accept fixtures are adversarial on purpose —
they are what a naive ``"car" in decision`` / ``re.search(r"train/")`` check
gets wrong:

  * ADR-0444 mentions ``train/`` and worktrees but states a general rule.
  * ADR-0460 uses generic ``car`` as a grammatical SUBJECT ("A deletion car
    must still move ratchets DOWN") — subject position alone is not the signal;
    a SPECIFIC car designator in the LEADING position is.
  * ADR-0465 puts a verb and ``car`` in one sentence ("The `pyproject.toml`
    edit lands with the LAST car") — verb-proximity matching fails here.

If a future car "simplifies" the pattern set, these three are what it must
still pass.
"""

from __future__ import annotations

import pytest

from yadgar.core.server.tools.adr_render import _car_shaped_decision_error

# ── REJECT fixture ─────────────────────────────────────────────────────────────

# ADR-0450's decision, verbatim.  Two independent tells: a LEADING ``Car-F``
# designator, and a ledger row closed as the decision's own consequence.
_ADR_0450_DECISION = (
    "Car-F (train/bug-bag-2-2026-08-23) ships TESTS for the floor: a pure-function "
    "unit test on `_enrichment_null_clauses` + an integration test via "
    "`update_memory_fields` capturing the SET clause. No new code, no "
    "re-derivation, no moving the floor into a different layer. Closes #94 in the "
    "ledger because the bug is structurally fixed and the test now locks the "
    "contract."
)

# ── ACCEPT fixtures (all verbatim; all adversarial) ────────────────────────────

# ADR-0444 — mentions ``train/`` and ``origin/train/<name>``; states a rule.
_ADR_0444_DECISION = (
    "When the orchestrator worktree is the canonical mirror for an external train "
    "ref AND has no uncommitted or local-unique commits, resync with `git reset "
    "--hard origin/train/<name>` instead of `git merge --ff-only`. The "
    "orchestrator branch IS the train's local mirror; the contract permits "
    "destructive aliasing."
)

# ADR-0460 — generic ``car`` as a grammatical subject, twice.
_ADR_0460_DECISION = (
    "Split the rule on WHY the number moved. NEVER re-baseline to absorb growth "
    "that should have been avoided — incidental bloat, a car's own sprawl, or debt "
    "it introduced. DO re-baseline when the growth is load-bearing and the "
    "alternative is deleting the reason: a security gate, a correctness guard, or "
    "the rationale that keeps either alive. When re-baselining: take the number "
    "from the gate's OWN measurement (e.g. check_complexity_allowlist.py's DRIFT "
    "report `recorded=X, current=Y`), never by adding a hand-computed delta; write "
    "the rationale in the file's existing style, saying what the added lines ARE; "
    "and state explicitly that the number is as-measured and NOT a "
    "review-and-approve of the size. A deletion car must still move ratchets DOWN "
    "— if a baseline needs editing after a deletion, it gets lowered."
)

# ADR-0464 — no car/train/ledger vocabulary at all; the plain control.
_ADR_0464_DECISION = (
    "Backward compatibility is NOT an acceptable justification for retaining code, "
    "a parameter, a wire field, a column, or an allowlist entry in this "
    "repository. There is one user, one deployment, and no external API consumer. "
    'Any artifact whose only defence is "an older/other caller might rely on it" '
    "is DELETABLE, and a reviewer should treat such a reason as a null argument "
    "rather than a weak one."
)

# ADR-0465 — verb + ``car`` in one sentence ("lands with the LAST car").
_ADR_0465_DECISION = (
    "Drop `BLE001` from `[tool.ruff.lint] ignore` and triage every site. Per site, "
    "exactly one outcome: NARROW it to the exception types actually expected "
    "(preferred — this is the outcome that finds swallowed bugs); or keep it broad "
    "WITH a stated reason at a genuine isolation boundary (atexit handlers, "
    "teardown that must not mask a test failure, daemon loops that must not die, "
    "the drainer's transient classifier); or report it as a FINDING when narrowing "
    "would change behaviour and no honest reason exists. A blanket pass that "
    "applies `# noqa: BLE001` to all 660 is explicitly a FAILED outcome — it "
    "reproduces the exact state this decision ends. The `pyproject.toml` edit "
    "lands with the LAST car or with the integrator, never first, because a lone "
    "un-ignore turns the lint gate red for every other car in flight."
)

# ADR-0463 — an ADR about ADR FORM; names the rewrite mechanism, not a car.
_ADR_0463_DECISION = (
    "When an ADR's DECISION, rationale and alternatives are unchanged and only its "
    "FORM is wrong — narrative where a decision belongs, mechanism that should live "
    "in a wiki page, prose that reads as a session memory — rewrite the body in "
    'place. Unlock with wiki_set_mutability(value="free"), rewrite, re-lock.'
)

_ACCEPT_FIXTURES: dict[str, str] = {
    "ADR-0444": _ADR_0444_DECISION,
    "ADR-0460": _ADR_0460_DECISION,
    "ADR-0464": _ADR_0464_DECISION,
    "ADR-0465": _ADR_0465_DECISION,
    "ADR-0463": _ADR_0463_DECISION,
}


# ── The gate itself ────────────────────────────────────────────────────────────


def test_car_shaped_decision_is_rejected() -> None:
    """ADR-0450's decision — a car as subject — must not pass the gate."""
    err = _car_shaped_decision_error(_ADR_0450_DECISION)
    assert err is not None, "ADR-0450's car-subject decision passed the gate"
    assert err["ok"] is False
    assert isinstance(err["error"], str)


def test_refusal_message_teaches_the_rule() -> None:
    """The refusal must name the fault AND the fix, not just say 'invalid'.

    It also has to quote the fragment that tripped it — a caller cannot fix
    prose the gate will not point at.
    """
    err = _car_shaped_decision_error(_ADR_0450_DECISION)
    assert err is not None
    message = err["error"].lower()
    # Names the fault.
    assert "decision" in message
    assert any(word in message for word in ("car", "train", "ledger"))
    # Names the fix: state the rule, move the incident to context.
    assert "rule" in message
    assert "context" in message
    # Quotes the offending fragment so the caller can see what tripped it.
    assert "car-f" in message


@pytest.mark.parametrize(("adr_id", "decision"), sorted(_ACCEPT_FIXTURES.items()))
def test_legitimate_decisions_are_accepted(adr_id: str, decision: str) -> None:
    """Real corpus decisions must pass — a false positive blocks a real write."""
    err = _car_shaped_decision_error(decision)
    assert err is None, f"{adr_id} was wrongly rejected: {err}"


def test_ledger_close_phrasing_is_rejected_on_its_own() -> None:
    """ "Closes #NNN in the ledger" is a tell even without a leading car."""
    decision = (
        "The drainer classifies a duplicate as a rejection rather than a failure. "
        "Closes #212 in the ledger."
    )
    assert _car_shaped_decision_error(decision) is not None


def test_generic_ledger_prose_is_accepted() -> None:
    """Mentioning the ledger is not naming a ledger row as the subject."""
    decision = (
        "Tasks are archived, never deleted, so a completed ledger row stays "
        "readable after the train that closed it is merged."
    )
    assert _car_shaped_decision_error(decision) is None


def test_empty_decision_is_not_the_gate_s_problem() -> None:
    """An empty/blank decision is a missing-required-field error upstream.

    The shape gate must stay silent on it so the caller gets the accurate
    diagnosis from ``_validate_adr_add_input``'s required-field loop.
    """
    assert _car_shaped_decision_error("") is None
    assert _car_shaped_decision_error("   \n  ") is None


# ── Wiring: the gate is reached from the tool's validator ──────────────────────


def _valid_payload(decision: str) -> dict[str, str]:
    """A payload that passes every pre-existing check, varying only ``decision``."""
    return {
        "title": "A title",
        "status": "accepted",
        "date": "2026-08-28",
        "context": "Some background.",
        "decision": decision,
        "rationale": "Because.",
        "alternatives": "Considered and rejected X.",
        "consequences": "Y follows.",
        "revisit_trigger": "If Z changes.",
        "supersedes": "none",
    }


def test_validator_rejects_car_shaped_decision() -> None:
    from yadgar.core.server.tools.adr import _validate_adr_add_input

    err = _validate_adr_add_input(_valid_payload(_ADR_0450_DECISION))
    assert err is not None
    assert err["ok"] is False


def test_validator_accepts_real_decisions() -> None:
    from yadgar.core.server.tools.adr import _validate_adr_add_input

    for adr_id, decision in _ACCEPT_FIXTURES.items():
        assert _validate_adr_add_input(_valid_payload(decision)) is None, (
            f"{adr_id} was wrongly rejected by _validate_adr_add_input"
        )


def test_missing_field_still_wins_over_shape() -> None:
    """A blank decision reports the required-field error, not the shape error."""
    from yadgar.core.server.tools.adr import _validate_adr_add_input

    payload = _valid_payload("")
    err = _validate_adr_add_input(payload)
    assert err is not None
    assert "missing required field" in err["error"]
    assert "decision" in err["error"]
