# 04 — Give the report the engine tool (finding 9)

**Status: shipped 2026-07-25 in `8c340d4` (wave 3), verified by
stubbed-SDK tests and boundary review. The first live run
(2026-07-27) exposed a gap — the tool was available but the prompt
carried no FENs to feed it — fixed by the wave-5 follow-up below.**

## Symptom

The report path is `complete()` with `max_turns=1` and no tools, so
the model asserts concrete lines it cannot verify — the single
biggest remaining quality lever (coach-report-improvements.md §9).
The machinery already exists: `explain` runs agentically against
`analyze_position` through `PositionAnalystFn`, in both shipped
providers, including the Copilot turn-budget guard. Turning points
deliberately carry no PV today because only a live engine call can
produce a trustworthy one.

## Decision: one agentic run, optional analyst, small turn budget

Chosen over a two-pass select-then-write shape: one run reuses the
`explain` machinery nearly verbatim in both providers, and the
report cache (finding 10, shipped) already ensures the more
expensive call is paid once per (player, agent, window,
PROMPT_VERSION).

## Contract change (main session, before delegating)

`CoachProvider.complete` gains an optional analyst:

```python
class CoachProvider(Protocol):
    async def complete(self, prompt: str,
                       analyst: PositionAnalystFn | None = None) -> str
    ...
```

With an analyst, providers expose it as the `analyze_position(fen)`
tool — the same mechanics as `explain` (in-process SDK MCP server
for Claude; custom tool registration for Copilot; no other tools
permitted) — under a report turn budget. With `None`, behavior is
today's single turn.

Update `docs/06-coach.md` (interface block, providers section, and
the turning-points paragraph that says entries state the move but
not the refutation — the model now verifies lines itself) and
`docs/07-api.md` (coach endpoint wiring). Same commit as the
`domain`/interface edit if any type moves.

## Slices

### coach-dev

- Both providers implement the new signature. Claude: agentic
  `query(...)` with the MCP-wrapped tool and a small `max_turns`;
  pin `_REPORT_MAX_TURNS = 8`. Copilot: reuse the explain budget
  pattern (grace wrap-up round, then cut off); same constant.
- `coach/prompt.py`: the closing instruction block gains the
  verification rule — check any concrete line with
  `analyze_position` before asserting it, when the tool is
  available; keep the report template itself unchanged. Bump
  `PROMPT_VERSION`, regenerate the snapshot, read the diff.
- Provider tests with the stubbed SDKs: analyst exposed as the only
  tool; turn budget enforced; `analyst=None` degrades to one turn;
  tool-loop transcripts still concatenate to the returned advice.

### api-dev (after coach-dev)

- `coach_player` (`api/routes.py`) builds the same analyst wrapper
  `explain_move` builds around the engine pool (depth/multipv stay
  the injector's choice) and passes it to `complete`; pass `None`
  when the pool is disabled.
- Tests: the analyst reaches the provider; the cache still serves
  the second call without a provider invocation; a `refresh` run
  re-invokes with the analyst.

Acceptance: both gates green; snapshot diff shows only the
instruction-block addition; report cache behavior unchanged.

## Notes

- Sequence after doc 03: both edit `coach/prompt.py` and the
  snapshot, and each bumps `PROMPT_VERSION`. Two waves, two bumps,
  two readable snapshot diffs.
- An agentic report is slower and costs more per generation; both
  are acceptable because generation is user-triggered and cached.
  If the Coach page's pending state feels too long on real data,
  note it — streaming the report is a separate feature, not this
  one.

## Live-run follow-up (wave 5)

The first live run (2026-07-27, Copilot agent, 45 s, 450 analyzed
rapid games) confirmed the machinery and exposed two defects in how
the prompt meets the tool. Wave 5 fixes both; the coverage half of
that wave lives in [07-analysis-coverage.md](07-analysis-coverage.md).

- **The prompt carried no FENs.** `analyze_position` takes a FEN, and
  the rendered report contained none — `CriticalPosition.fen` existed
  all along but `_turning_point_entry` never rendered it. The tool
  was available and structurally unusable: every "engine preferred X"
  in the output was the stored `best` field, and no entry could say
  *why* a move lost. Fix: render the FEN on each turning-point entry
  (backticked, as the explain prompt renders positions).
- **The verification instruction was defensive.** "Check any line
  before asserting it" is satisfied by asserting nothing new, which
  is exactly what the model did. Fix: affirmative and scoped — for
  each turning point the brief features, run the tool on that entry's
  FEN and state the refutation; the scope keeps the calls within
  `_REPORT_MAX_TURNS`.

One `PROMPT_VERSION` bump ("2026-07-fen-coverage") covers this
follow-up and 07's coverage statement, keeping the snapshot churn to
one readable diff per the README's sequencing rule.
