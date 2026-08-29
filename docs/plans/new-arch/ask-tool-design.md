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
- The answer is **fully detailed**. Brevity is not the goal — `recall` ships a
  pile of passages, `ask` answers the question. Lower context cost is a
  consequence of that, not the purpose.
- **The response carries no memory or wiki bodies.** Answer plus citation
  identifiers.
- **Claims are cited individually**, with inline markers, so a statement maps to
  the record it came from. A claim with no marker is visibly ungrounded.
- **Where sources disagree, the answer says so** and cites both. It never picks a
  winner silently.
- **No LLM state lives in a process.** Continuity travels as an opaque
  conversation token backed by the shared cache.
- **Behaviour is configuration**, and changes to it are gated on a scored
  evaluation set.
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

  // Opaque handle from a previous AskResponse (D29). The prior context lives in
  // the shared cache under this key with a TTL; no service process holds it.
  // Expired or evicted is not an error — the request is treated as a new
  // conversation and the response says so.
  optional string conversation_token = 5;
}

message Citation {
  // Marker used inline in the answer text, e.g. 1 renders as [1].
  uint32 marker = 1;
  string urn = 2;
  // What the record IS, not what it says — a slug, an ADR number, a title.
  // Capped short. Never a snippet.
  string label = 3;
  double relevance = 4;
  uint32 hop = 5;      // which hop surfaced it
}

// Two or more cited records that support conflicting claims (D32). Reported,
// never resolved silently.
message Disagreement {
  repeated uint32 markers = 1;   // the citations that conflict
  string description = 2;        // what they disagree about
}

message AskResponse {
  string answer = 1;
  repeated Citation citations = 2;

  // false = the LLM was unavailable or out of budget and this is a template
  // answer. Callers must be able to tell the two apart.
  bool synthesized = 3;

  repeated Disagreement disagreements = 4;

  // Pass back on the next ask to continue this conversation.
  string conversation_token = 5;
  // True when the supplied token had expired and this was treated as a fresh
  // conversation. Never let the caller believe context was carried when it was not.
  bool conversation_restarted = 6;

  uint32 hops_used = 7;
  bool deadline_hit = 8;
  AskTiming timing = 9;
}
```

There is deliberately no `raw_results` field. A caller that wants a record's
content fetches it by URN from the owning module's batch-get rpc, paying for
exactly what it asked for.

The answer text carries the markers inline:

> Stop the drainer first `[1]`, then run the scramble with `--dry-run` `[2]`. Note
> that `[2]` and `[3]` disagree on whether the backend must also be stopped.

This is what makes the identifiers useful rather than decorative. A wrong step
maps to one record, which can then be corrected at the source.

## Tuning

Prompts, hop cap, deadline, model choice and retrieval mix are **configuration**,
not constants — changing them is a config change, not a redeploy (D30).

No change is accepted on how it reads. A fixed evaluation set of questions with
known good answers and known correct citations is scored against a candidate
configuration, and that score is the justification. A prompt that reads better
frequently answers worse.

Two properties make this cheap:

- changing a prompt changes the generation cache key (D17), so old results cannot
  leak into a new configuration's scores and nothing needs invalidating;
- every `ask` logs its question, hops, citations, synthesis flag and timing — the
  operational signal and the source the evaluation set grows from.

## What it feeds back

Questions feed curation. Generated answers do not (D31). A question that could not
be answered, or that exhausted its hops, is a documented gap — evidence of
something actually needed rather than something merely stored. Citation frequency
marks load-bearing records; repeatedly retrieved but never cited marks noise. A
disagreement identifies both records involved and is the cleanest gap signal of
all.

An answer worth keeping is written deliberately as an ordinary memory or wiki
page, through the same gates as any other write — never persisted as a side
effect of having been generated.

## Per-user concerns

- **Conversation state** is owned by a user and visibility-scoped like everything
  else.
- **Budget.** A per-user ceiling on generation spend, enforced at the gateway, so
  one caller's iterative questions cannot starve others on the shared path.
- **Cache keys must NOT include a user id.** The generation cache is
  content-addressed on the prompt, and the prompt contains the retrieved context,
  which is already visibility-filtered. Two users with different reach produce
  different prompts and so different keys; two users who produce an identical
  prompt saw identical passages and are both entitled to the result. Adding a user
  id would destroy sharing on org-visible content while providing no safety that
  content-addressing does not already give.

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
