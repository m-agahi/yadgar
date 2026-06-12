"""TDD tests for v5.51.0 §4.6 — /api/stats TTL cache.

Tests verify:
- Two calls within TTL → second served from cache (get_memory_stats called once)
- After TTL expiry → recompute (get_memory_stats called again)
- STATS_CACHE_TTL_S=0 disables cache (get_memory_stats always called)
- cache_age_seconds present in response (0 on fresh compute, >0 on cache hit)
- Different project params do not collide (separate cache buckets)
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch


def _make_stats_request(project: str | None = None) -> MagicMock:
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, d=None: project if k == "project" else d)
    return req


def _parse_response(resp) -> dict:
    """Parse JSONResponse body to dict."""
    body = resp.body if hasattr(resp, "body") else b"{}"
    return json.loads(body)


class TestStatsCacheHitWithinTTL:
    """Second call within TTL uses cached data, not a fresh compute."""

    def test_cache_hit_within_ttl(self):
        """get_memory_stats is called only once for two requests within TTL."""
        import yadgar.server.http as _http

        call_count = {"n": 0}

        def _fake_get_memory_stats():
            call_count["n"] += 1
            return {"total_memories": call_count["n"], "custom": "data"}

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.side_effect = _fake_get_memory_stats

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                return fn(*a, **kw)

            req = _make_stats_request()
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=60),
                    ):
                        # Clear cache before test
                        _http._stats_cache.clear()

                        resp1 = await _http.api_stats(req)
                        resp2 = await _http.api_stats(req)
            return resp1, resp2

        resp1, resp2 = asyncio.run(_run())

        assert call_count["n"] == 1, (
            f"get_memory_stats called {call_count['n']} times, expected 1. "
            "Second request within TTL must use cached result."
        )

        body1 = _parse_response(resp1)
        body2 = _parse_response(resp2)
        assert "cache_age_seconds" in body1, "cache_age_seconds missing from response"
        assert "cache_age_seconds" in body2, "cache_age_seconds missing from cached response"
        assert body1["cache_age_seconds"] == 0, (
            f"First response cache_age_seconds should be 0, got {body1['cache_age_seconds']}"
        )
        assert body2["cache_age_seconds"] >= 0, (
            f"Second response cache_age_seconds should be >= 0, got {body2['cache_age_seconds']}"
        )


class TestStatsCacheMissAfterTTL:
    """After TTL expires, get_memory_stats is called again."""

    def test_cache_miss_after_ttl(self):
        """After TTL, a new request triggers recompute."""
        import yadgar.server.http as _http

        call_count = {"n": 0}

        def _fake_get_memory_stats():
            call_count["n"] += 1
            return {"total_memories": call_count["n"]}

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.side_effect = _fake_get_memory_stats

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                return fn(*a, **kw)

            req = _make_stats_request()
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=1),
                    ):
                        _http._stats_cache.clear()
                        resp1 = await _http.api_stats(req)
                        # Manually expire cache by backdating cached_at
                        _http._stats_cache["cached_at"] = time.monotonic() - 2
                        resp2 = await _http.api_stats(req)
            return resp1, resp2

        resp1, resp2 = asyncio.run(_run())

        assert call_count["n"] == 2, (
            f"get_memory_stats called {call_count['n']} times after TTL expiry. "
            "Expected 2 (one before, one after TTL)."
        )


class TestStatsCacheDisabledAtZero:
    """STATS_CACHE_TTL_S=0 disables caching entirely."""

    def test_cache_disabled_when_ttl_zero(self):
        """With TTL=0, every request calls get_memory_stats (no caching)."""
        import yadgar.server.http as _http

        call_count = {"n": 0}

        def _fake_get_memory_stats():
            call_count["n"] += 1
            return {"total_memories": call_count["n"]}

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.side_effect = _fake_get_memory_stats

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                return fn(*a, **kw)

            req = _make_stats_request()
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=0),
                    ):
                        _http._stats_cache.clear()
                        await _http.api_stats(req)
                        await _http.api_stats(req)

        asyncio.run(_run())

        assert call_count["n"] == 2, (
            f"With STATS_CACHE_TTL_S=0, get_memory_stats called {call_count['n']} times. "
            "Expected 2 (caching disabled — no TTL means no cache)."
        )


class TestStatsCacheDifferentProjectsDontCollide:
    """Cache with project=A must not serve results for project=B."""

    def test_different_project_params_get_separate_cache_results(self):
        """Requests with different project params each trigger their own compute."""
        import yadgar.server.http as _http

        call_log = []

        def _fake_get_memory_stats():
            return {"total_memories": len(call_log) + 1}

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.side_effect = _fake_get_memory_stats

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                call_log.append(1)
                return fn(*a, **kw)

            req_a = _make_stats_request(project="proj-a")
            req_b = _make_stats_request(project="proj-b")
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=60),
                    ):
                        _http._stats_cache.clear()
                        await _http.api_stats(req_a)
                        await _http.api_stats(req_b)

        asyncio.run(_run())

        assert len(call_log) == 2, (
            f"Expected 2 compute calls (proj-a and proj-b separately), got {len(call_log)}. "
            "Different project params must NOT collide in cache."
        )

    def test_same_project_within_ttl_hits_cache(self):
        """Same project param within TTL uses cache."""
        import yadgar.server.http as _http

        call_log = []

        def _fake_get_memory_stats():
            return {"total_memories": 42}

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.side_effect = _fake_get_memory_stats

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                call_log.append(1)
                return fn(*a, **kw)

            req1 = _make_stats_request(project="same-proj")
            req2 = _make_stats_request(project="same-proj")
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=60),
                    ):
                        _http._stats_cache.clear()
                        await _http.api_stats(req1)
                        await _http.api_stats(req2)

        asyncio.run(_run())

        assert len(call_log) == 1, (
            f"Expected 1 compute call for same project within TTL, got {len(call_log)}. "
            "Same project within TTL must hit cache."
        )


class TestStatsCacheAgeSeconds:
    """cache_age_seconds field is present and correct in all cases."""

    def test_cache_age_seconds_zero_on_fresh_compute(self):
        """Fresh compute returns cache_age_seconds=0."""
        import yadgar.server.http as _http

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.return_value = {"total_memories": 10}

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                return fn(*a, **kw)

            req = _make_stats_request()
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=60),
                    ):
                        _http._stats_cache.clear()
                        return await _http.api_stats(req)

        resp = asyncio.run(_run())
        body = _parse_response(resp)
        assert "cache_age_seconds" in body, "cache_age_seconds missing from response"
        assert body["cache_age_seconds"] == 0, (
            f"Fresh compute must return cache_age_seconds=0, got {body['cache_age_seconds']}"
        )

    def test_cache_age_seconds_present_on_hit(self):
        """Cached response has cache_age_seconds present (non-negative)."""
        import yadgar.server.http as _http

        mock_storage = MagicMock()
        mock_storage.get_memory_stats.return_value = {"total_memories": 10}

        async def _run():
            async def _fake_to_thread(fn, *a, **kw):
                return fn(*a, **kw)

            req = _make_stats_request()
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("yadgar.server.http._st") as mock_st:
                    mock_st._storage = mock_storage
                    with patch(
                        "yadgar.config.get_settings",
                        return_value=MagicMock(STATS_CACHE_TTL_S=60),
                    ):
                        _http._stats_cache.clear()
                        await _http.api_stats(req)
                        return await _http.api_stats(req)

        resp = asyncio.run(_run())
        body = _parse_response(resp)
        assert "cache_age_seconds" in body, "cache_age_seconds missing from cached response"
        assert body["cache_age_seconds"] >= 0, (
            f"cache_age_seconds must be non-negative, got {body['cache_age_seconds']}"
        )
