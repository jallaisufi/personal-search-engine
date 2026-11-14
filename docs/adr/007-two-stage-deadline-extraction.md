# ADR 007: Deadline extraction in two stages, with dates parsed by code

**Status:** accepted · 2026-11

## Context

Documents contain deadlines ("submit by 31 July 2027"). Finding them needs judgement (is this date a deadline or just an event?), which suits a language model. But sending every passage to a model is slow and, with a small local model, unreliable: models misread and miscalculate dates, and a wrong date in a deadline list is worse than a missing one.

## Decision

**Stage 1: rules, no model.** `deadline_rules.py` parses dates with regular expressions (ISO, "20 November 2026", "November 20th", "31. Juli 2027", "31.07.2027", "end of February 2027"; dates without a year resolve to their next occurrence relative to the document's modified date). A date is a *candidate* only if its sentence also contains a cue word (due, by, until, deadline, submit, expires, Frist, bis, spätestens, ...).

**Stage 2: the model, only on candidates.** For each passage with candidates, the model gets the passage and the list of parsed dates and returns JSON: which dates are deadlines, and a short task for each. The answer is validated: a date that is not in the candidate list is dropped.

So the split of responsibilities is:

| | Decided by |
|---|---|
| Which date, exactly | Code (deterministic, unit-tested) |
| Where it is in the document | Code (character offsets of the sentence) |
| Is it a real deadline? What has to be done? | Model |

**A separate queue job.** Extraction is queued after a document is indexed (`task: "deadlines"`), with more retries than indexing, because on a first start the chat model may still be downloading. Indexing and search never wait for the model, and a failed extraction never marks the document as failed.

**Dismissals survive re-indexing.** Deadlines belong to the document, not only to a chunk (chunks are replaced on every re-index). A dismissed deadline is matched by date and sentence, so it does not come back when the document changes elsewhere.

## Consequences

- Model calls scale with the number of passages that mention a dated obligation, not with the size of the collection. `/stats` reports passages scanned vs sent to the model.
- The model cannot hallucinate or shift a date, and every deadline links to the exact sentence it came from.
- Recall is bounded by stage 1: deadlines without an explicit date ("within two weeks", "next Friday") or in unsupported languages are missed. Relative dates are future work; resolving them reliably needs care about what they are relative to.
- Cue words are deliberately broad ("by"), so stage 1 over-selects and the model filters. With the deterministic fake model used in CI every candidate is accepted, which tests the pipeline but not the model's judgement.
