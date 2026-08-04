# Coach game search: event definitions spike

Run 2026-08-03. Answers the definitions question in
[archive/coach-game-search.md](../archive/coach-game-search.md):
do the scan events discriminate on real data, and can event
composition capture the target dialogue's queen-sacrifice game while
rejecting its two named near-misses?

**Verdict in one line:** yes, over two passes: the sacrifice event
finds the target game, rejects both near-misses for the right
reasons, and sweeps 605 analyzed wins in ~4 s at under 7 ms/game;
composition chains ("queen sac then mate within 12", "castled long
then queen sac") match the real plies. The first pass added a
realization *gate*; the second pass measured that gate's recall
cost, found it hides genuinely good games, and demoted realization
to an annotation while replacing the binary new-offer gate with an
escalation test.

## Hardware and environment

| | |
|---|---|
| Machine | Apple M4 Max, 36 GB unified memory |
| OS | macOS 15.7.7 |
| Python | CPython 3.12.11 (backend venv via uv) |
| python-chess | 1.11.2 |
| Database | production `data/coach.sqlite3`, read-only, 2026-08-03 |
| Archive | 8,200 games (4,091 wins), 1,253 analyzed at version 2, depth 16 |

## Method

A prototype detector importing the SEE helpers from
`coach/highlights.py` **verbatim** (`_best_exchange_gain`,
`_see_gain`, `_captured_value`), so the numbers measure the
production sacrifice definition, not a reimplementation. Events:

- `sacrifice` (first-pass definition; the second pass below revises
  (b) to an escalation test and demotes (d) to an annotation): all
  of (a) not a promotion; (b) new offer: before the move, the
  opponent's best SEE gain via a null move is < 2 (gate passes when
  the mover was in check); (c) offer: opponent's best SEE gain
  after the move, minus what the move captured, >= 2, with the
  piece tier read from the SEE target square; (d) realized: player
  POV material drops >= 2 below its pre-move level within 6 plies,
  or the player mates inside that window. Carries eval before/after
  (stored, player POV), `sound` (eval after >= 0) and
  `balanced_before` (eval before <= +2.0).
- `eval_swing`: consecutive-ply stored-eval delta >= 3.0 pawns,
  player POV, mate folded.
- `comeback`: win with player POV eval <= -3.0 at some ply.
- `delivered_mate`: win and the final replayed board is checkmate.
- `castled`: the player's O-O / O-O-O, exact from replay.

Composition: an ordered event sequence, each step optionally within
N plies of the previous match.

## Raw results: the three dialogue games

The target dialogue names one positive and two near-misses, all in
the archive and analyzed (opponent handles removed):

| Game | Expected | Sacrifice events found |
|---|---|---|
| 2026-03-07 game | queen sac, mate | ply 39 `Qxg7+`, queen, net 6, eval +9.2 → #5, sound, not balanced-before |
| 27...Qxe1+ while winning | none | none: answering the check cannot win the queen, so SEE finds no offer |
| 15...Nxd4 combination | none rook+ | none rook+ (one net-2 minor event at ply 12, correctly small) |

Composition matches on the 2026-03-07 game, against real plies:

- `sacrifice(queen)` then `delivered_mate` within 12: plies 39 → 49
- `castled(long)` then `sacrifice(queen)`: plies 37 → 39 (the real
  19.O-O-O before 20.Qxg7+)
- On the Qxe1+ game, no sacrifice chain fires, but `comeback` does at
  ply 46 (worst stored eval −14.4 in a won game), which is that
  game's true story: the dialogue's "king hunt" was a conversion
  after the opponent cracked.

## Raw results: archive sweep of rook-or-queen sacrifices in wins

605 analyzed wins scanned per configuration; times are single-thread.

| Configuration | Games with events | Sound | Sac → mate chain |
|---|---|---|---|
| Offer gates only (first run) | 80 | 60 | 26 |
| + promotion exclusion + realization gate | 50 | 37 | 14 |

3.9 s total, 6.4 ms/game, including the realization replays.

What the first run exposed, and the gates fixed:

- **Promotions dominated the false positives.** `a8=Q` reads to SEE
  as a queen en prise for net 4+; roughly half the "sound queen
  sacrifices" were endgame promotions. Excluded outright.
- **Won-position noise realizes too.** Quiet king moves that abandon
  a rook while +10 survive every gate (the material really does go),
  e.g. `Ke3` at +5.7 → +5.7. These are marked, not removed:
  `balanced_before` is false on all of them and the SAN itself
  (`Ke3` vs `Qxg7+`) is decisive for a model reading candidates.

Precision of what remains: the sound list is check-capture attacking
sacs (`Qxg7+` #5, `Qg5+` #3 → #8, `Rxg6+` +10.6 → +21.8) plus the
marked king-move residue. Only 3 of 605 wins show a sound sacrifice
from a roughly balanced position, which independently confirms the
dialogue coach's verdict that sacrificing is not a hallmark of this
player's play.

## Second pass: recall cost of the gates

The first pass optimized for precision; review raised the retrieval
counter-argument: the model reads the candidates anyway, so a gate
that hides a good game costs more than an annotation that flags a
bad one. Measured by emitting every rook+ SEE offer (promotion
excluded) with annotations, then decomposing what each gate
suppresses. Same archive, same hardware, 6.9 ms/game.

| Configuration (rook+ offers, promotions excluded) | Games |
|---|---|
| No further gate | 116 |
| + binary new-offer gate (nothing >= 2 already hanging) | 64 |
| + realization required within 6 plies | 50 |
| + realization required within 12 plies | 53 |
| + realization required, any distance | 57 |
| Escalation gate instead, no realization filter (final) | 60 |

What the realized-within-6 filter hides: 14 of the 64 gated games
vanish entirely, and they are not all noise. Among them: a **sound
declined exchange sac from an equal position** (`Rxg6+`, +0.1
staying +0.1, never taken), two **winning swindle sacrifices** from
roughly balanced positions that realized late (`Qb3+` +1.1 to −7.2,
realizing at ply 14; a declined queen offer at +0.8 to −7.3,
realizing at ply 21), and a near-miss that realized at ply 7, one
past the window. These are exactly the games a coach conversation
about sacrifices wants to surface. Widening the window (12, or
unbounded) recovers only part of the list, because declined offers
never realize at all.

The binary new-offer gate has its own recall hole: it blocks any
sacrifice played while >= 2 points of material already hung, which
suppressed 59 realized events, most of them cash-ins or blunders
but at least one balanced rook sac among them. The escalation form
(the move must *raise* the opponent's best SEE gain by >= 2) keeps
the per-ply dedup that motivated the gate, readmits 4 events (all
blunder-shaped, correctly annotated `sound=false`) and drops 7
marginal net-2 ones. On the three dialogue games the final
configuration is identical to the first pass: `Qxg7+` fires exactly
once (realizes in 1), both near-misses stay silent.

## What this changes in the design

- Sacrifice gates are **escalation, piece tier, promotion
  exclusion**. Realization, soundness, balance and the eval pair
  are **annotations on every match** (`realizes: N | declined`),
  never filters; `sound_only` stays as an opt-in parameter.
- The candidate volume still validates the tool shape: 605 wins
  reduce to 60 flagged games, few enough for the model to triage by
  SAN and flags, many enough that unscreened reading could never
  cover them.
- The latency claim in the design (3-10 ms/game) measures at
  6.4-6.9 ms/game on this hardware across both passes.

## Not measured

- **Recall.** No ground-truth labeling of the archive exists; the
  spike proves the named negatives are rejected and the named
  positive found, not that no true sacrifice is missed.
- **One player's archive.** Ratings around 1100-1300 rapid; the
  event thresholds may read differently on stronger players' games.
- **`balanced_before` on genuine brilliancies.** The first pass
  found 3 balanced sound candidates (king-move-shaped); the second
  pass adds a handful of balanced declined and swindle sacs. With
  no ground-truth labels, the flag's value on true balanced
  brilliancies is asserted from the definition, not measured.
- **eval_swing and castled precision** beyond the case games; both
  are exact computations, but their usefulness as composition
  primitives on wider queries is untested.
