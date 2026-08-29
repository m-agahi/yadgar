# `ask` — the default retrieval tool

Status: **DRAFT**. Companion to `architecture-decisions-2026-08-29.md` (D28, D29).
Replaces the "answer-first recall" section of the deleted
`llm-service-design-2026-08-02.md`, whose latency budget, deadline propagation and
degradation state machine are carried forward here with the tenancy framing removed.

## TL;DR

- `ask` becomes the **default** tool an instance calls. `recall` stays as the
  primitive `ask` uses, the fallback when the LLM is down, and the escape hatch
  for callers wanting raw hits.
- An internal LLM receives the question, **drives retrieval itself**, and answers.
- **Iterative, with a hop cap** — the LLM may re-query after seeing results.
- **The response carries no memory or wiki bodies.** Answer plus citation
  identifiers only. Cutting the caller's token cost is the entire point of the
  tool; returning passages would defeat it.
- The LLM is **stateless between calls** and stateful only within one request.
- It never fails: LLM down or out of budget degrades to a template answer with
  `synthesized: false`.

## Why it exists

A `recall` call returns passages, and the caller pays for every one of them in
context whether or not they bear on the question. Most do not. Moving synthesis
behind the boundary means the caller pays for one answer and a list of
identifiers instead of a ranked pile of prose.

## Shape

```proto
message AskRequest {
  yadgar.common.v1.Scope scope = 1;
  string question = 2;

  // Total budget. The gateway sets a default; retrieval takes its share first
  // and the LLM gets the remainder.
  uint32 deadline_ms = 3;

  // Hop cap. 1 = plan once, retrieve once, answer.
  uint32 max_hops = 4;

  // Caller-supplied prior context, when a follow-up needs it (D29). Explicit
  // and auditable, because the service keeps none of its own.
  repeated string prior_turns = 5;
}

message Citation {
  string urn = 1;
  // What the record IS, not what it says — a slug, an ADR number, a title.
  // Capped short. Never a snippet.
  string label = 2;
  double relevance = 3;
  uint32 hop = 4;      // which hop surfaced it
}

message AskResponse {
  string answer = 1;
  repeated Citation citations = 2;

  // false = the LLM was unavailable or out of budget and this is a template
  // answer. Callers must be able to tell the two apart.
  bool synthesized = 3;

  uint32 hops_used = 4;
  bool deadline_hit = 5;
  AskTiming timing = 6;
}
```

There is deliberately no `raw_results` field. A caller that wants a record's
content fetches it by URN from the owning module's batch-get rpc, paying for
exactly what it asked for.

## The hop loop

1. LLM receives the question and the available providers.
2. It emits a query. `recall` fans out and returns candidates.
3. The LLM either answers, or emits another query having seen what came back.
4. Repeat until it answers, `max_hops` is reached, or the deadline is spent.

The cap is not a tuning knob but a safety property: without it, a question the
corpus cannot answer becomes an unbounded loop against the one latency-critical
path in the system.

Hop context lives for the duration of one request and is discarded when it
returns (D29).

## Budget

Inherited from the deleted draft, still the target:

| Stage | Budget |
|---|---|
| retrieval (embed ~20ms, fanout + fusion ~50ms, rerank ~50ms) | ~120ms |
| LLM synthesis | ~800ms |
| overhead | ~10ms |
| **total, single hop** | **~1s** |

`deadline_ms` propagates from the gateway. Retrieval is allocated first; the LLM
receives the remainder and is **cancelled** rather than permitted to overrun.
Each additional hop costs another retrieval plus another generation, which is why
the cap and the deadline are both enforced rather than either alone.

## Degradation

- LLM answers in time → `{answer, citations, synthesized: true}`.
- LLM times out, errors, or is unavailable → the top-ranked citations are
  returned with a template answer and `synthesized: false`.
- A hop that returns nothing does not fail the call; the LLM answers from what it
  has, or says it cannot.

`ask` never returns an error for a retrieval or synthesis shortfall. It degrades.

## Prompt discipline

Temperature 0. Bounded output. The system prompt requires the model to say it
does not have enough information rather than invent an answer, and to cite the
identifier for every claim.

## The honest cost

Returning identifiers instead of passages means the caller **cannot check the
answer without a second call.** Synthesis is a hallucination surface that raw
`recall` does not have, and this design removes the cheap local check. Three
things bound it, none of which eliminate it:

- citations are mandatory and per-claim, so an unsupported statement is visible
  as an uncited one;
- `label` says what each cited record is, so an obviously irrelevant citation is
  detectable without fetching;
- `recall` remains available when a caller wants to see the evidence itself.

This is a deliberate trade of verifiability for context cost, and it should be
revisited if answers prove unreliable in practice.
