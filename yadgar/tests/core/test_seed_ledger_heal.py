"""The seeder heals the ledger rows of the slugs it owns (ADR-0217 / ADR-0218).

The defect: ``seed_agent_prompts`` is create-if-absent per wiki SLUG, and the
ledger row was only ever written as a side effect of creating the page. A page
that already existed when the ledger table arrived therefore never got a row,
and no later seed run could repair it — ``agent_prompt_get("pr-review")``
returned ``not_found`` on a live install while the page was right there. The
same path never recorded ``baseline_hash``, so ADR-0217's three-way upgrade
merge had no ancestor to compare against.

What the seeder now guarantees, for every packaged slug (starters, the
contract, the disciplines) and ONLY those:

* a page with no ledger row gets one, pinned to the LIVE page bytes;
* ``baseline_hash`` is the hash of the packaged content, recorded only when the
  live page still IS that content — a page that differs could have been seeded
  from an older package, so claiming the current one as its ancestor would be
  a provenance the seeder does not have;
* an existing row is never rewritten, except to fill a NULL ``baseline_hash``
  with the same proof, keeping every other column as stored;
* a failed ledger read heals nothing (an empty set would read as "no row
  exists anywhere" and rewrite every row).

The open-ended corpus (pages the package does not own) stays with the human-run
procedure ``docs/prompts/backfill-agent-pattern-ledger.md`` — ADR-0005.
"""

from __future__ import annotations

import pytest

from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("seed_ledger_heal")
    server.init_engines(
        db_path=str(tmp_path / "test_seed_ledger_heal.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


class _Ledger:
    """In-memory stand-in for the two engine-#2 tables (no MariaDB in unit tests).

    The upserts mirror ``MariaStorageEngine``: keyed on ``name``, every column
    overwritten except ``baseline_hash``, which is COALESCEd.
    """

    def __init__(self) -> None:
        self.patterns: dict[str, dict] = {}
        self.disciplines: dict[str, dict] = {}
        self.list_fails = False
        self.writes: list[tuple[str, str]] = []

    @staticmethod
    def _upsert(table: dict[str, dict], row: dict) -> dict:
        prior = table.get(row["name"], {})
        merged = {**prior, **row}
        merged["baseline_hash"] = row.get("baseline_hash") or prior.get("baseline_hash")
        table[row["name"]] = merged
        return merged

    def save_pattern(self, payload: dict) -> dict:
        self.writes.append(("agent_pattern", payload["name"]))
        return self._upsert(
            self.patterns,
            {
                "name": payload["name"],
                "body_slug": payload["body_slug"],
                "content_hash": payload["content_hash"],
                "purpose": payload.get("purpose"),
                "status": payload.get("status", "active"),
                "baseline_hash": payload.get("baseline_hash"),
                "uses": self.patterns.get(payload["name"], {}).get("uses", 0),
            },
        )

    def save_discipline(self, payload: dict) -> dict:
        self.writes.append(("agent_discipline", payload["name"]))
        meta = payload.get("meta") or {}
        return self._upsert(
            self.disciplines,
            {
                "name": payload["name"],
                "body_slug": payload["body_slug"],
                "content_hash": payload["content_hash"],
                "baseline_hash": payload.get("baseline_hash"),
                "purpose": meta.get("purpose"),
                "always_applied": bool(meta.get("always_applied", False)),
                "position": int(meta.get("position", 0)),
                "status": meta.get("status", "active"),
            },
        )

    def list_patterns(self, payload: dict) -> dict:
        if self.list_fails:
            return {"ok": False, "error": "engine #2 not composed"}
        return {"rows": list(self.patterns.values())}

    def list_disciplines(self, payload: dict) -> dict:
        if self.list_fails:
            return {"ok": False, "error": "engine #2 not composed"}
        return {"rows": list(self.disciplines.values())}


@pytest.fixture
def ledger():
    import yadgar.backend.admin_exec as _exec

    led = _Ledger()
    replacements = {
        "save_agent_pattern_row": led.save_pattern,
        "save_agent_discipline_row": led.save_discipline,
        "list_agent_prompt_rows": led.list_patterns,
        "list_agent_discipline_rows": led.list_disciplines,
    }
    originals = {k: _exec._ADMIN_OPS[k] for k in replacements}
    _exec._ADMIN_OPS.update(replacements)
    yield led
    _exec._ADMIN_OPS.update(originals)


@pytest.fixture
def storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


def _seed(storage):
    from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

    return seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)


def _packaged():
    """(table, name, slug, packaged-content hash) for every slug the package owns."""
    from yadgar.core.server.tools.agent_prompts import (
        CONTRACT_GENESIS,
        CONTRACT_SLUG,
        DISCIPLINE_SLUG_PREFIX,
        DISCIPLINES,
        STARTER_PROMPTS,
        _content_hash,
        _wrap_prompt_page,
    )

    out = [
        ("agent_pattern", p, f"agent-prompt-{p}", _content_hash(_wrap_prompt_page(pu, c)))
        for p, pu, c in STARTER_PROMPTS
    ]
    p, pu, c = CONTRACT_GENESIS
    out.append(("agent_pattern", p, CONTRACT_SLUG, _content_hash(_wrap_prompt_page(pu, c))))
    out += [
        (
            "agent_discipline",
            n,
            f"{DISCIPLINE_SLUG_PREFIX}{n}",
            _content_hash(_wrap_prompt_page(pu, c)),
        )
        for n, pu, c in DISCIPLINES
    ]
    return out


def _table(ledger, table):
    return ledger.patterns if table == "agent_pattern" else ledger.disciplines


class TestFreshSeedRecordsBaseline:
    def test_every_packaged_row_carries_the_packaged_hash(self, storage, ledger):
        _seed(storage)
        for table, name, slug, packaged in _packaged():
            row = _table(ledger, table).get(name)
            assert row is not None, f"{table} row missing for {name}"
            assert row["body_slug"] == slug
            assert row["baseline_hash"] == packaged, f"{name}: baseline_hash not recorded"
            assert row["content_hash"] == packaged


class TestExistingPageWithoutRow:
    def test_reseed_creates_the_missing_rows(self, storage, ledger):
        _seed(storage)  # pages exist (module-scoped DB); rows now present
        ledger.patterns.clear()
        ledger.disciplines.clear()

        result = _seed(storage)

        assert result["created"] == 0, "pages must stay create-if-absent"
        for table, name, _slug, packaged in _packaged():
            row = _table(ledger, table).get(name)
            assert row is not None, f"{table} row for existing page {name} was not healed"
            assert row["baseline_hash"] == packaged
        assert result["ledger_rows_created"] == len(_packaged())

    def test_contract_row_lives_in_agent_pattern(self, storage, ledger):
        """The contract's row is where ``agent_prompt_save`` writes it, not a second one."""
        _seed(storage)
        assert "contract" in ledger.patterns
        assert "contract" not in ledger.disciplines

    def test_second_heal_writes_nothing(self, storage, ledger):
        _seed(storage)
        ledger.writes.clear()
        result = _seed(storage)
        assert ledger.writes == []
        assert result["ledger_rows_created"] == 0
        assert result["ledger_baselines_set"] == 0


class TestExistingRowsAreNotRewritten:
    def test_null_baseline_is_filled_and_other_columns_kept(self, storage, ledger):
        _seed(storage)
        row = ledger.patterns["pr-review"]
        row.update(baseline_hash=None, purpose="operator wording", status="active", uses=7)

        result = _seed(storage)

        healed = ledger.patterns["pr-review"]
        assert healed["baseline_hash"] is not None
        assert healed["purpose"] == "operator wording"
        assert healed["uses"] == 7
        assert result["ledger_baselines_set"] >= 1

    def test_failed_ledger_read_heals_nothing(self, storage, ledger):
        _seed(storage)
        ledger.patterns.clear()
        ledger.disciplines.clear()
        ledger.list_fails = True
        ledger.writes.clear()

        result = _seed(storage)

        assert ledger.writes == [], "a failed read must not be treated as an empty ledger"
        assert result["ledger_error"]


class TestEditedPageGetsNoClaimedAncestor:
    """Last in the module: it edits a page in the shared module-scoped DB."""

    def test_edited_page_row_has_live_hash_and_no_baseline(self, storage, ledger):
        from yadgar.core.server.tools.agent_prompts import (
            STARTER_PROMPTS,
            _content_hash,
            _read_agent_prompt,
            agent_prompt_save,
        )

        _seed(storage)
        seed_body = next(c for p, _, c in STARTER_PROMPTS if p == "drift-audit")
        agent_prompt_save(
            "drift-audit",
            seed_body + "\n- a locally added rule",
            directory="global",
            purpose="local purpose",
            project=TEST_PROJECT_ID,
        )
        ledger.patterns.clear()

        result = _seed(storage)

        live = _read_agent_prompt("agent-prompt-drift-audit", storage=storage)
        row = ledger.patterns["drift-audit"]
        assert row["content_hash"] == _content_hash(live["content"])
        assert row["baseline_hash"] is None
        assert row["purpose"] == "local purpose"
        assert "drift-audit" in result["ledger_baseline_unknown"]


class TestGlobalReachTag:
    """Car C7: reach is the ``global`` TAG. The core wrappers attach it themselves.

    ``WikiStore.add`` also adds it for library page types, but the backend op's
    ``_st._wiki is None`` arm writes the payload's tags verbatim — so the tag
    must already be on the payload, whichever arm runs.
    """

    def _capture(self, monkeypatch):
        import yadgar.core.server.tools.agent_prompts as m

        sent: list[tuple[str, dict]] = []

        def _fake(op, payload, timeout_s=30.0):
            sent.append((op, payload))
            return {"saved": True, "version": 1, "slug": payload.get("slug")}

        monkeypatch.setattr(m, "_forward_admin", _fake)
        return sent

    def test_agent_prompt_save_payload_carries_global(self, monkeypatch):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        sent = self._capture(monkeypatch)
        agent_prompt_save("x-pattern", "body", directory="/tmp/repo", project=TEST_PROJECT_ID)
        page = next(p for op, p in sent if op == "agent_prompt_save")
        assert "global" in page["tags"]

    def test_discipline_save_payload_carries_global(self, monkeypatch):
        from yadgar.core.server.tools.agent_prompts import _save_discipline_page

        sent = self._capture(monkeypatch)
        _save_discipline_page("x-disc", "purpose", "body", project_id=TEST_PROJECT_ID)
        page = next(p for op, p in sent if op == "agent_prompt_save")
        assert "global" in page["tags"]
