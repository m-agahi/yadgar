#!/usr/bin/env python3
"""Capture a yadgar MCP-tool span-tree from Tempo and dump it as JSON.

The capture half of the span -> diagram pipeline (companion to
``docs/diagrams/generate.py``, which renders a YAML spec to SVG/DOT). Given a
tool's boundary span name it finds that tool's most-recent trace in Tempo,
fetches the full span table, and writes a flat, rel-timed JSON the diagram
author (or a future auto-spec generator) can transcribe from.

Prereqs: yadgar exports OTLP to a live Tempo (``otlp_endpoint`` in
``~/.config/yadgar/config.yaml``), Tempo query API reachable (default
http://localhost:3200). Spans export off-thread (BatchSpanProcessor) so allow
a few seconds after the call before capturing; this script retries the search.

Every MCP tool is wrapped ``@_tool()`` -> ``trace_span("tool.<name>")``
(server/_app.py), so the boundary span name is ``tool.<registered-tool-name>``
(e.g. ``tool.recall``, ``tool.project_brief``, ``tool.wiki_add``).

Usage:
    # fire the MCP tool, then:
    python docs/diagrams/capture_trace.py tool.recall out.json [since_epoch] [label]

    # cold vs warm: capture right after a fresh call, then after an immediate repeat
    S=$(date +%s); <call tool.recall>; python .../capture_trace.py tool.recall recall-cold.json $S recall-cold

Output JSON: {label, tool_span, trace_id, total_ms, span_count,
              spans:[{rel_ms, dur_ms, depth, svc, name}, ...]}  (start-ordered)

Blind-spot reading: span_count <= 2 => boundary-only (tool internals
un-instrumented); no trace => tool emits no span; span_count in the thousands
=> per-item span explosion (see task #48). Wall time lives in ``total_ms``;
un-attributed time = parent dur minus summed child durs.

Exit codes: 0 ok; 2 search failed; 3 no trace found; 4 fetch failed; 5 empty trace.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

TEMPO = "http://localhost:3200"


def _get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def _find_trace(name, since, now):
    """Newest trace-id containing boundary span `name`, or None. Retries (async export)."""
    q = urllib.parse.urlencode({"q": f'{{name="{name}"}}', "start": since, "end": now, "limit": 10})
    for _attempt in range(3):
        traces = _get(f"{TEMPO}/api/search?{q}").get("traces", [])
        if traces:
            traces.sort(key=lambda t: -int(t.get("startTimeUnixNano", 0)))
            return traces[0]["traceID"]
        time.sleep(3)
    return None


def _extract_spans(trace_json):
    """Flatten a Tempo trace into rel-timed, depth-tagged, start-ordered spans."""
    spans = []
    for b in trace_json.get("batches", []):
        attrs = b.get("resource", {}).get("attributes", [])
        svc = next(
            (a["value"].get("stringValue", "") for a in attrs if a["key"] == "service.name"), ""
        )
        for ss in b.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans.append(
                    {
                        "id": s["spanId"],
                        "parent": s.get("parentSpanId", ""),
                        "name": s["name"],
                        "svc": svc,
                        "start": int(s["startTimeUnixNano"]),
                        "end": int(s["endTimeUnixNano"]),
                    }
                )
    if not spans:
        return spans
    t0 = min(s["start"] for s in spans)
    byid = {s["id"]: s for s in spans}

    def depth(s):
        d_, p = 0, s["parent"]
        while p and p in byid and d_ < 30:
            d_, p = d_ + 1, byid[p]["parent"]
        return d_

    for s in spans:
        s["rel_ms"] = round((s["start"] - t0) / 1e6, 2)
        s["dur_ms"] = round((s["end"] - s["start"]) / 1e6, 2)
        s["depth"] = depth(s)
    spans.sort(key=lambda s: s["start"])
    return spans


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    name, out = sys.argv[1], sys.argv[2]
    since = int(sys.argv[3]) if len(sys.argv) > 3 else int(time.time()) - 300
    label = sys.argv[4] if len(sys.argv) > 4 else name

    try:
        tid = _find_trace(name, since, int(time.time()) + 5)
    except Exception as e:  # noqa: BLE001 — CLI, surface + exit
        print(f"SEARCH FAIL {name}: {e}")
        return 2
    if not tid:
        print(f"NO TRACE for {name} since {since} (tool emitted no span, or export lag)")
        return 3
    try:
        spans = _extract_spans(_get(f"{TEMPO}/api/traces/{tid}"))
    except Exception as e:  # noqa: BLE001
        print(f"FETCH FAIL {tid}: {e}")
        return 4
    if not spans:
        print(f"EMPTY {tid}")
        return 5

    total_ms = round(max(s["rel_ms"] + s["dur_ms"] for s in spans), 1)
    json.dump(
        {
            "label": label,
            "tool_span": name,
            "trace_id": tid,
            "total_ms": total_ms,
            "span_count": len(spans),
            "spans": [
                {k: s[k] for k in ("rel_ms", "dur_ms", "depth", "svc", "name")} for s in spans
            ],
        },
        open(out, "w"),
        indent=1,
    )
    print(f"OK {label}: {tid[:16]} {len(spans)} spans {total_ms:.0f}ms -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
