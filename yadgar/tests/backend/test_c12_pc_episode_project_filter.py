"""C12 (0047 PR#40 §5) — ``pc.py``'s episode filter is keyed on ``project_id``.

**The bug this pins.** ``_fetch_filtered_episodes`` filtered
``e["directory"] == project_id`` — a filesystem PATH column compared against an
``owner/repo`` IDENTITY. Those two never compare equal, so the filter matched
**zero episodes** whenever ``project_id`` was real, and ``build_event_matrix``
returned an empty matrix rather than a scoped one. It failed silently: an empty
matrix is a legitimate "not enough data" outcome, so nothing raised.

It was latent rather than live — the only production caller,
``CausalDiscovery.discover_dag()`` via ``_run_causal_discovery_phase``, passes no
``project_id`` at all, so the branch was skipped. The public facade
(``CausalDiscovery.build_event_matrix(project_id=…)``) is where it bites.

C11's migration 033 gave ``episode`` a ``project_id`` column and
``insert_episode`` stamps it, which is what makes the fix available.

**The documented gap, pinned below so a later car cannot "fix" it wrongly.**
``_shared/runtime/recall_session.py`` is the one ``capture_action`` caller with
no project in scope — correctly so; threading one would mean changing
``_apply_recall_session_side_effects``'s signature up the core recall path. An
episode whose ONLY captures came from recall therefore carries
``project_id=None`` and is excluded here.

A dual-arm ``project_id OR directory`` filter was considered and REJECTED as
dead code, on two independent measurements:

  1. ``directory`` holds a path and ``project_id`` holds ``owner/repo`` — the
     legacy arm can never match a real ``project_id``, so it would add nothing.
  2. ``ActionLogger.capture`` assigns ``episode["directory"] = directory``
     UNCONDITIONALLY, and recall passes ``""``. Any episode a recall touched has
     had its ``directory`` blanked, so the legacy arm cannot even recover the
     historical corpus. (That blanking is a real defect in the legacy column
     C11's dual-write depends on — reported upward, not fixed here.)

So the honest shape is: re-key onto ``project_id``, and STATE the gap. These
tests are what make it stated rather than silent.
"""

from __future__ import annotations

from yadgar.backend.causal_discovery.pc import _fetch_filtered_episodes

_PROJECT = "m-agahi/yadgar"
_OTHER_PROJECT = "someone-else/other-repo"

#: The caller's filesystem path. Deliberately NOT equal to ``_PROJECT``: a test
#: reusing one string for both passes under either keying.
_PATH = "/home/max/git/yadgar"

_CUTOFF = "2026-08-01T00:00:00+00:00"


def _episode(eid: int, *, project_id: str | None, directory: str, ts: str) -> dict:
    return {
        "id": eid,
        "timestamp": ts,
        "directory": directory,
        "project_id": project_id,
        "raw_content": f"episode {eid}",
    }


class _FakeStorage:
    def __init__(self, episodes: list[dict]) -> None:
        self._episodes = episodes

    def get_episodes_since(self, episode_id: int):  # noqa: ARG002
        return list(self._episodes)


def _ids(episodes: list[dict]) -> list[int]:
    return [e["id"] for e in episodes]


class TestProjectScopedFilter:
    """A real ``project_id`` must select that project's episodes — not zero of them."""

    def test_selects_episodes_stamped_with_the_project(self) -> None:
        storage = _FakeStorage(
            [
                _episode(1, project_id=_PROJECT, directory=_PATH, ts="2026-08-05T00:00:00+00:00"),
                _episode(
                    2,
                    project_id=_OTHER_PROJECT,
                    directory="/somewhere/else",
                    ts="2026-08-06T00:00:00+00:00",
                ),
            ]
        )
        assert _ids(_fetch_filtered_episodes(storage, _CUTOFF, _PROJECT)) == [1]

    def test_a_path_valued_directory_does_not_defeat_the_match(self) -> None:
        """The pre-C12 shape returned [] here — ``/home/...`` never equals ``owner/repo``."""
        storage = _FakeStorage(
            [
                _episode(1, project_id=_PROJECT, directory=_PATH, ts="2026-08-05T00:00:00+00:00"),
            ]
        )
        assert _fetch_filtered_episodes(storage, _CUTOFF, _PROJECT) != []

    def test_excludes_another_projects_episodes(self) -> None:
        storage = _FakeStorage(
            [
                _episode(
                    1,
                    project_id=_OTHER_PROJECT,
                    directory=_PATH,
                    ts="2026-08-05T00:00:00+00:00",
                ),
            ]
        )
        assert _fetch_filtered_episodes(storage, _CUTOFF, _PROJECT) == []

    def test_a_directory_equal_to_the_project_id_is_not_a_match(self) -> None:
        """The legacy arm is GONE, not merely unreachable — identity is the only key."""
        storage = _FakeStorage(
            [
                _episode(1, project_id=None, directory=_PROJECT, ts="2026-08-05T00:00:00+00:00"),
            ]
        )
        assert _fetch_filtered_episodes(storage, _CUTOFF, _PROJECT) == []


class TestUnstampedEpisodesAreTheDocumentedGap:
    """Recall-originated and pre-033 episodes carry no ``project_id``. Stated, not silent."""

    def test_an_unstamped_episode_is_excluded_when_a_project_is_named(self) -> None:
        storage = _FakeStorage(
            [
                _episode(1, project_id=None, directory="", ts="2026-08-05T00:00:00+00:00"),
                _episode(2, project_id=_PROJECT, directory=_PATH, ts="2026-08-06T00:00:00+00:00"),
            ]
        )
        assert _ids(_fetch_filtered_episodes(storage, _CUTOFF, _PROJECT)) == [2]

    def test_a_row_missing_the_key_entirely_does_not_raise(self) -> None:
        """Pre-033 rows have no ``project_id`` key — the old hard subscript would KeyError."""
        storage = _FakeStorage(
            [{"id": 1, "timestamp": "2026-08-05T00:00:00+00:00", "raw_content": "legacy"}]
        )
        assert _fetch_filtered_episodes(storage, _CUTOFF, _PROJECT) == []

    def test_unstamped_episodes_survive_when_no_project_is_named(self) -> None:
        """``project_id=None`` means 'no scoping' — the whole-corpus path must keep them."""
        storage = _FakeStorage(
            [
                _episode(1, project_id=None, directory="", ts="2026-08-05T00:00:00+00:00"),
                _episode(2, project_id=_PROJECT, directory=_PATH, ts="2026-08-06T00:00:00+00:00"),
            ]
        )
        assert _ids(_fetch_filtered_episodes(storage, _CUTOFF, None)) == [1, 2]


class TestCutoffAndOrderingAreUnchanged:
    """The re-key must not disturb the two behaviours that were already correct."""

    def test_episodes_before_the_cutoff_are_dropped(self) -> None:
        storage = _FakeStorage(
            [
                _episode(1, project_id=_PROJECT, directory=_PATH, ts="2026-07-01T00:00:00+00:00"),
                _episode(2, project_id=_PROJECT, directory=_PATH, ts="2026-08-05T00:00:00+00:00"),
            ]
        )
        assert _ids(_fetch_filtered_episodes(storage, _CUTOFF, _PROJECT)) == [2]

    def test_result_is_sorted_by_timestamp(self) -> None:
        storage = _FakeStorage(
            [
                _episode(1, project_id=_PROJECT, directory=_PATH, ts="2026-08-09T00:00:00+00:00"),
                _episode(2, project_id=_PROJECT, directory=_PATH, ts="2026-08-05T00:00:00+00:00"),
            ]
        )
        assert _ids(_fetch_filtered_episodes(storage, _CUTOFF, _PROJECT)) == [2, 1]
