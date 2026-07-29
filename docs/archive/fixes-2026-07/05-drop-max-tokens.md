# 05 — Remove the inert `LlmConfig.max_tokens`

**Status: shipped 2026-07-25 in `f90f97e` (wave 1).**

## Symptom

`LlmConfig.max_tokens` (`domain.py`) is read by no shipped
provider — both ride a local CLI login rather than an API call that
takes a token ceiling — so setting it in `coach.config.yaml` does
nothing. A config knob that silently does nothing is a trap; the
field even documents its own inertness in a comment.

## Decision: drop it, don't wire it

Wiring it would mean inventing a meaning for two providers whose
SDKs don't take an output-token ceiling. If the planned `anthropic`
API provider lands, a token ceiling returns as a provider-specific
setting with real behavior. Until then the honest contract is the
absence of the field. Pydantic ignores unknown keys by default, so
an existing `coach.config.yaml` that still sets `max_tokens`
degrades exactly as today: it does nothing.

## Scope: main session only — no sub-agent

The change is smaller than a delegation round trip. References are
confined to:

- `backend/src/chess_coach/domain.py` — delete the field and its
  comment from `LlmConfig` (`CoachAgent` inherits the removal).
- `backend/tests/test_api.py` — the only test file naming it;
  adjust fixtures.
- Docs: check `docs/01-config.md` and `docs/06-coach.md` for any
  mention of the field (none expected beyond example YAML);
  `docs/archive/coach-report-improvements.md` §8 mentions the default —
  leave that doc alone, it is a historical record.

Acceptance: `make check` green; `grep -r max_tokens backend web`
finds nothing outside the historical improvements doc.
