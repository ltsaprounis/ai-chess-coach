"""The `scan_games` event library (docs/06-coach.md, "Chat").

Every fixture is a real, legal SAN sequence from the standard start
position -- `run_scan` always replays from `chess.Board()`, mirroring
`build_highlights`' own replay -- verified move by move with
python-chess while building these fixtures (see
docs/spike-reports/coach-game-search-events.md for the measured
definitions this suite pins). The sacrifice event's gates (escalation,
tier, promotion) are the bulk of this file; `eval_swing`, `comeback`,
`delivered_mate`, `castled` and the chain matcher follow in their own
sections below.
"""

from collections.abc import Sequence
from typing import Literal

import chess
import pytest

import chess_coach.coach.scan as scan_module
from chess_coach.coach import run_scan
from chess_coach.domain import (
    Color,
    MoveEval,
    Result,
    ScanCandidate,
    ScanEventSpec,
    ScanSpec,
)
from tests.factories import make_game, summarize

# --- fixtures ---------------------------------------------------------
#
# Légal's Mate: White's 5.Nxe5 unguards the queen on d1 (the knight was
# its only blocker on the g4-d1 diagonal), offering it for a pawn;
# Black grabs it (5...Bxd1) and White mates two moves later. Verified:
# ply 9 (Nxe5) captures a pawn (1), the opponent's best reply nets 6 via
# Bxd1 (Kxd1 recaptures the bishop, so Black nets queen-for-bishop, 6),
# for a net offer of 5; nothing was hanging beforehand (gain_before=0),
# so escalation is 5; the position after Nxe5 is not checkmate, so the
# realization walk applies -- Black's very next move captures the queen
# outright, dropping White's own material by 9, well past the 2-point
# floor, at offset 1.
LEGALS_MATE_MOVES = [
    "e4", "e5", "Nf3", "d6", "Bc4", "Bg4", "Nc3", "g6", "Nxe5", "Bxd1",
    "Bxf7+", "Ke7", "Nd5#",
]  # fmt: skip

# A held queen: the same opening through the offering move (ply 9), but
# Black ignores it for two more of White's own moves (Nf6, then White
# plays d4 and h3) instead of capturing -- the escalation gate must not
# re-fire on either, since the offer's size is unchanged both times.
HELD_QUEEN_MOVES = [
    "e4", "e5", "Nf3", "d6", "Bc4", "Bg4", "Nc3", "g6", "Nxe5", "Nf6",
    "d4", "c6", "h3",
]  # fmt: skip

# The same shape, but a White pawn is already hanging (b4, attacked by
# a5) before the knight sacrifices the queen -- escalation still fires
# (offer 5 minus the pre-existing 1-point pawn hang is 4), where a
# binary "nothing already hanging" gate would have refused to fire at
# all just because something else was already en prise.
SAC_WHILE_PAWN_HANGS_MOVES = [
    "e4", "e5", "Nf3", "d6", "Bc4", "Bg4", "Nc3", "g6", "b4", "a5", "Nxe5",
]  # fmt: skip

# An exchange sacrifice: White's rook lift reaches c6 down the newly
# opened c-file and captures a knight defended only by a pawn -- net 2
# (rook 5 minus knight 3), the exchange-sac floor exactly.
EXCHANGE_SAC_MOVES = [
    "c4", "d5", "cxd5", "Qxd5", "Nc3", "Qd8", "d4", "Nc6", "Bf4", "e6",
    "Rc1", "Be7", "Nb5", "Nf6", "Rxc6",
]  # fmt: skip

# An ordinary favourable capture -- White's e-pawn takes Black's d-pawn
# and nothing further is offered (SEE nets 0 on the recapture): not a
# sacrifice by any definition.
FAVOURABLE_CAPTURE_MOVES = ["e4", "d5", "exd5"]

# The Qxe1+ shape from the design doc: a capture-with-check where the
# capturing piece is immune (protected by the bishop on c4), so
# answering the check cannot win it back -- SEE finds no offer at all.
# This particular instance also happens to be Scholar's Mate; escalation
# fails regardless (offer_after is negative), which is checked before
# `_check_sacrifice` ever looks at whether the move delivered mate.
NO_OFFER_CHECK_MOVES = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]

# A promotion that, were it not excluded outright, would read as a
# queen sacrifice: the a-pawn (relabelled from White's b-pawn, which
# captured Black's a-pawn along the way) walks to a7 unopposed and then
# captures Black's still-unmoved b8 knight, promoting -- landing a new,
# undefended queen the a8 rook could win right back (net 6, escalation
# 5 against the sole pre-existing hang, a loose a7 pawn worth 1).
PROMOTION_MOVES = [
    "b3", "a5", "Bb2", "a4", "bxa4", "g6", "a5", "g5", "a6", "Nf6", "a7",
    "d6",
]  # fmt: skip


def _candidate(
    game_id: str,
    san_moves: Sequence[str],
    *,
    color: Color = "white",
    result: Result = "win",
    analyzed: bool = False,
    eval_overrides: dict[int, dict[str, object]] | None = None,
) -> ScanCandidate:
    """A `ScanCandidate` replaying `san_moves` for real, mirroring
    `test_coach_highlights.py::_analyzed_game`'s flat-baseline-plus-
    overrides shape: every ply gets `eval_cp=0, cp_loss=0,
    judgment="best"` unless `eval_overrides` (1-based ply) says
    otherwise. `analyzed=False` (the default) leaves `evals=None`, the
    unanalyzed-game shape `scan_candidates` returns (docs/03-storage.md).
    """
    game = make_game(id=game_id, color=color, san_moves=list(san_moves), result=result)
    summary = summarize(game, analyzed=analyzed)
    evals: list[MoveEval] | None = None
    if analyzed:
        overrides = eval_overrides or {}
        board = chess.Board()
        rows: list[MoveEval] = []
        for idx, san in enumerate(san_moves):
            ply = idx + 1
            move = board.parse_san(san)
            fields: dict[str, object] = {
                "ply": ply,
                "san": san,
                "eval_cp": 0,
                "eval_mate": None,
                "best_move": move.uci(),
                "cp_loss": 0,
                "judgment": "best",
            }
            fields.update(overrides.get(ply, {}))
            rows.append(MoveEval.model_validate(fields))
            board.push(move)
        evals = rows
    return ScanCandidate(summary=summary, san_moves=list(san_moves), evals=evals)


def _spec(
    *, piece: Literal["queen", "rook", "minor"] = "minor", sound_only: bool = False
) -> ScanSpec:
    return ScanSpec(
        match=[ScanEventSpec(event="sacrifice", piece=piece, sound_only=sound_only)]
    )


# --- the offer: escalation, tier, promotion ----------------------------


def test_queen_sac_matches_and_realizes_next_ply() -> None:
    candidate = _candidate("legal", LEGALS_MATE_MOVES)

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 9
    assert hit.san == "Nxe5"
    assert "queen sac, net 5" in hit.detail
    assert "gave the queen for a pawn" in hit.detail
    assert "realizes in 1" in hit.detail


def test_exchange_sac_matches_rook_tier_despite_netting_only_two() -> None:
    candidate = _candidate("exch", EXCHANGE_SAC_MOVES)

    matches = run_scan([candidate], _spec(piece="rook"))

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 15
    assert hit.san == "Rxc6"
    assert "rook sac, net 2" in hit.detail
    assert "gave the rook for a knight" in hit.detail


def test_queen_tier_filter_excludes_a_rook_offer() -> None:
    """ "rook" matches rook or queen; "queen" matches queen only
    (docs/06-coach.md)."""
    candidate = _candidate("exch", EXCHANGE_SAC_MOVES)

    assert run_scan([candidate], _spec(piece="queen")) == []
    assert run_scan([candidate], _spec(piece="minor")) != []


def test_minor_tier_filter_matches_a_queen_offer_as_minor_or_better() -> None:
    candidate = _candidate("legal", LEGALS_MATE_MOVES)

    assert run_scan([candidate], _spec(piece="minor")) != []
    assert run_scan([candidate], _spec(piece="rook")) != []


def test_favourable_capture_does_not_match() -> None:
    candidate = _candidate("fav", FAVOURABLE_CAPTURE_MOVES)

    assert run_scan([candidate], _spec(piece="minor")) == []


def test_check_the_opponent_must_answer_with_no_offer_does_not_match() -> None:
    """The Qxe1+ shape (docs/06-coach.md): a capture-with-check whose
    capturing piece cannot be won back produces no SEE offer at all."""
    candidate = _candidate("noOffer", NO_OFFER_CHECK_MOVES)

    assert run_scan([candidate], _spec(piece="minor")) == []


def test_promotion_never_matches() -> None:
    candidate = _candidate("promo", PROMOTION_MOVES)

    assert run_scan([candidate], _spec(piece="minor")) == []


def test_declined_offer_matches_with_realizes_declined() -> None:
    """Truncating the game right at the offering move -- no further
    moves exist to realize or decline it in fact, only in this fixture
    -- is the simplest way to exercise `_realization`'s "never dropped,
    never mated" fallthrough (docs/06-coach.md: "declined" when it
    never does)."""
    candidate = _candidate("declined", LEGALS_MATE_MOVES[:9])

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    assert "declined" in matches[0].hits[0].detail
    assert "realizes in" not in matches[0].hits[0].detail


def test_held_queen_fires_once_and_never_re_fires() -> None:
    """The escalation gate dedups by construction: while the queen sits
    untaken across two more of the player's own moves, neither
    re-registers as a new offer (docs/06-coach.md)."""
    candidate = _candidate("held", HELD_QUEEN_MOVES)
    step = ScanEventSpec(event="sacrifice", piece="queen")

    occurrences = scan_module._sacrifice_occurrences(  # pyright: ignore[reportPrivateUsage]
        candidate, step
    )

    assert [occ.ply for occ in occurrences] == [9]


def test_sac_played_while_a_pawn_already_hangs_still_matches() -> None:
    """Escalation, not a binary "nothing hanging" gate: the offer still
    registers on top of a pre-existing 1-point pawn hang."""
    candidate = _candidate("pawnhang", SAC_WHILE_PAWN_HANGS_MOVES)

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    assert matches[0].hits[0].ply == 11


def test_in_check_ply_counts_prior_gain_as_zero() -> None:
    """A hand-built position (not a from-start game -- this drives
    `_check_sacrifice` directly, the same private-access convention
    test_coach_chat.py uses for its tool handlers): White's king is in
    check from a bishop, and a rook on a1 already hangs to a black rook
    on a8, unrelated to the check. Escaping with a bare king move does
    not touch the rook, so its offer is identical before and after --
    a correctly-computed gain_before (5, ignoring the check) would zero
    out the escalation and refuse to fire. The rule forces gain_before
    to 0 instead, and this move fires anyway, proving the rule is what
    fired it.
    """
    board = chess.Board("r3k3/8/8/8/1b6/8/8/R3K3 w - - 0 1")
    assert board.is_check()
    move = board.parse_san("Kf1")
    step = ScanEventSpec(event="sacrifice", piece="minor")

    hit = scan_module._check_sacrifice(  # pyright: ignore[reportPrivateUsage]
        board, move, 0, "Kf1", [], None, True, step
    )

    assert hit is not None
    assert "rook sac, net 5" in hit.detail


# --- annotations: sound, balanced_before, eval pair, unanalyzed --------


def test_balanced_before_and_sound_annotations_from_crafted_evals() -> None:
    candidate = _candidate(
        "annotated",
        EXCHANGE_SAC_MOVES,
        analyzed=True,
        eval_overrides={
            14: {"eval_cp": 20},  # eval before ply 15: balanced (<=200)
            15: {"eval_cp": 50},  # eval after: sound (>=0)
        },
    )

    matches = run_scan([candidate], _spec(piece="rook"))

    detail = matches[0].hits[0].detail
    assert "sound" in detail and "unsound" not in detail
    assert "balanced before" in detail
    assert "eval +0.20 -> +0.50" in detail


def test_already_winning_before_and_unsound_annotations() -> None:
    candidate = _candidate(
        "already-winning",
        EXCHANGE_SAC_MOVES,
        analyzed=True,
        eval_overrides={
            14: {"eval_cp": 250},  # eval before ply 15: > 200
            15: {"eval_cp": -50},  # eval after: unsound (< 0)
        },
    )

    matches = run_scan([candidate], _spec(piece="rook"))

    detail = matches[0].hits[0].detail
    assert "unsound" in detail
    assert "already winning before" in detail
    assert "eval +2.50 -> -0.50" in detail


def test_mate_score_renders_as_hash_n_in_the_eval_pair() -> None:
    candidate = _candidate(
        "matedetail",
        LEGALS_MATE_MOVES,
        analyzed=True,
        eval_overrides={9: {"eval_cp": None, "eval_mate": 5}},
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert "eval +0.00 -> #5" in matches[0].hits[0].detail


def test_unanalyzed_candidate_still_matches_with_unverified_annotation() -> None:
    candidate = _candidate("unanalyzed", EXCHANGE_SAC_MOVES, analyzed=False)

    matches = run_scan([candidate], _spec(piece="rook"))

    assert len(matches) == 1
    assert "unverified (unanalyzed)" in matches[0].hits[0].detail


def test_sound_only_drops_unsound_analyzed_matches() -> None:
    candidate = _candidate(
        "unsound",
        EXCHANGE_SAC_MOVES,
        analyzed=True,
        eval_overrides={15: {"eval_cp": -50}},
    )

    assert run_scan([candidate], _spec(piece="rook", sound_only=True)) == []
    assert run_scan([candidate], _spec(piece="rook", sound_only=False)) != []


def test_sound_only_keeps_unanalyzed_matches_recall_first() -> None:
    """docs/06-coach.md: on unanalyzed games sound is unknown, so
    `sound_only` keeps the match rather than dropping an unverifiable
    one."""
    candidate = _candidate("unanalyzed", EXCHANGE_SAC_MOVES, analyzed=False)

    matches = run_scan([candidate], _spec(piece="rook", sound_only=True))

    assert len(matches) == 1
    assert "unverified (unanalyzed)" in matches[0].hits[0].detail


# --- mate-aware verdict: a sacrifice offered inside a forced mate ------
#
# Mate-score folding makes the plain sound/unsound read (`eval_after >=
# 0`) blind to a piece donated inside an already-winning mating net: a
# mate that only slows down still folds positive and reads "sound" (the
# live bug this taxonomy fixes -- docs/06-coach.md, "Chat"). It only
# engages when `eval_before` is itself a mate for the player, so every
# fixture below overrides both the offering move's own ply and the one
# before it (the `eval_before` read) -- same craft-the-evals approach as
# `test_balanced_before_and_sound_annotations_from_crafted_evals` above.
# The position only has to produce a genuine, single sacrifice match;
# the mate story is entirely in the crafted evals.

# White's knight retreats to g1 -- undoing its own development rather
# than capturing -- which unblocks the exact same g4-d1 diagonal
# LEGALS_MATE_MOVES' Nxe5 does: Black's Bxd1 nets queen-for-bishop (6),
# White recaptures Kxd1. The only difference from LEGALS_MATE_MOVES is
# that Ng1 captures nothing, so `captured_by_move == 0` -- the "hangs
# the queen for nothing" shape the live bug (57...Qg5+) was found in.
# Verified: ply 9 (Ng1), queen tier, net 6, ply 9's own capture "nothing".
QUEEN_HANG_MOVES = [
    "e4", "e5", "Nf3", "d6", "Bc4", "Bg4", "Nc3", "g6", "Ng1",
]  # fmt: skip

# A random-but-legal walk (generated and verified with python-chess --
# the shape only needs to be a genuine queen sacrifice that CAPTURES a
# rook, not book theory) ending in White's Qxg6+, recapturing a rook
# Black had lifted to g6 earlier: net 4 (opponent's best reply nets
# queen-for-rook, minus the rook this move itself captured). It is the
# only queen-tier occurrence in the game, so eval crafting at plies
# 28/29 lands exactly on this move.
QUEEN_TAKES_ROOK_MOVES = [
    "a4", "g5", "g4", "a6", "c4", "b5", "Na3", "Nh6", "f4", "Rg8", "e4",
    "Nc6", "Be2", "bxc4", "e5", "Nxg4", "Nf3", "Rg6", "Qc2", "Nb4", "Qb1",
    "f6", "fxg5", "d6", "Nb5", "Ra7", "Ng1", "Nh6", "Qxg6+",
]  # fmt: skip

# Another random-but-legal walk, Black's Qxg5 capturing a bishop -- the
# real archive "Qxg7+" brilliancy's material profile (queen for a
# minor). Also the game's only queen-tier occurrence, so eval crafting
# at plies 33/34 lands exactly on this move.
QUEEN_TAKES_BISHOP_MOVES = [
    "a3", "f6", "c4", "g5", "d4", "h6", "Nh3", "d5", "Qd2", "Qd7", "Qe3",
    "b5", "Qc3", "Kf7", "g4", "e5", "Ng1", "a6", "Be3", "Qxg4", "Nd2",
    "b4", "Kd1", "Kg7", "Bxg5", "Kf7", "f4", "Ne7", "Qe3", "Ng8", "Kc1",
    "e4", "Qxe4", "Qxg5",
]  # fmt: skip


def test_slip_when_a_real_sacrifice_slows_a_forced_mate() -> None:
    """The live bug, shape 1 (docs/06-coach.md): mate-in-3 before the
    move, the queen offered for nothing, mate-in-8 after -- still
    "sound" by the plain eval-sign read (mate folds positive either
    way), but a queen given away for nothing inside a mating net is a
    slip the position happened to absorb, not technique, and must never
    print the word "sound"."""
    candidate = _candidate(
        "queen-hang-slows",
        QUEEN_HANG_MOVES,
        analyzed=True,
        eval_overrides={
            8: {"eval_cp": None, "eval_mate": 3},
            9: {"eval_cp": None, "eval_mate": 8},
        },
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 9
    # The giveaway ("gave the queen for nothing") already appears in the
    # net clause ahead of this one; the slip clause itself must not
    # repeat it.
    assert hit.detail == (
        "queen sac, net 6 (gave the queen for nothing); declined; "
        "a slip absorbed by a mating position: mate slowed #3 -> #8"
    )
    assert "sound" not in hit.detail
    assert run_scan([candidate], _spec(piece="queen", sound_only=True)) == []
    assert run_scan([candidate], _spec(piece="queen", sound_only=False)) != []


def test_slip_when_a_real_sacrifice_throws_away_the_forced_mate() -> None:
    """The live bug, shape 2 (the abheeghosal archive shape): the same
    "captures nothing" offer, but the mate is lost outright rather than
    merely slowed -- still a slip, never "sound"."""
    candidate = _candidate(
        "queen-hang-lost",
        QUEEN_HANG_MOVES,
        analyzed=True,
        eval_overrides={
            8: {"eval_cp": None, "eval_mate": 3},
            9: {"eval_cp": 1880, "eval_mate": None},
        },
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    detail = matches[0].hits[0].detail
    assert detail == (
        "queen sac, net 6 (gave the queen for nothing); declined; "
        "a slip absorbed by a mating position: threw away the forced "
        "mate, #3 -> +18.80"
    )
    assert "sound" not in detail
    assert run_scan([candidate], _spec(piece="queen", sound_only=True)) == []


def test_forced_home_when_a_capture_keeps_the_mate_at_least_as_fast() -> None:
    """The Qxe6+ simplification shape: mate-in-8 before, a real capture
    (a rook), mate-in-6 after -- material given up on the way to a mate
    that arrives no slower forces the mate home, rendered positively
    rather than with the plain "sound" wording, and still counts as
    sound for the filter."""
    candidate = _candidate(
        "queen-takes-rook",
        QUEEN_TAKES_ROOK_MOVES,
        analyzed=True,
        eval_overrides={
            28: {"eval_cp": None, "eval_mate": 8},
            29: {"eval_cp": None, "eval_mate": 6},
        },
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 29
    assert "forced the mate home: #8 -> #6" in hit.detail
    assert "gave the queen for a rook" in hit.detail
    assert "sound" not in hit.detail
    assert run_scan([candidate], _spec(piece="queen", sound_only=True)) != []


def test_non_mate_before_rendering_is_unchanged() -> None:
    """The regression baseline (the "today's gem" archive shape,
    Qxg7+): `eval_before` is a plain cp figure, not a mate, so the
    mate-aware taxonomy must never engage -- the detail renders exactly
    as it did before this taxonomy existed."""
    candidate = _candidate(
        "queen-takes-bishop",
        QUEEN_TAKES_BISHOP_MOVES,
        color="black",
        analyzed=True,
        eval_overrides={
            33: {"eval_cp": -920, "eval_mate": None},
            34: {"eval_cp": None, "eval_mate": -5},
        },
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 34
    assert hit.detail == (
        "queen sac, net 5 (gave the queen for a bishop); declined; "
        "sound; already winning before; eval +9.20 -> #5"
    )
    assert run_scan([candidate], _spec(piece="queen", sound_only=True)) != []


def test_forced_home_counterfactual_never_reclassifies_as_a_slip() -> None:
    """The regression this taxonomy exists to guard against: the SAME
    moves as the previous test's genuine brilliancy, but crafted as if
    a deeper future re-analysis now sees the mate before the sacrifice
    was even played (#6, where the shallower analysis above saw only
    +9.20 -- exactly what happens the next time ANALYSIS_VERSION bumps
    and the archive is re-analyzed at greater depth). The move still
    captures a bishop, so it must land on "forced the mate home", never
    on "slip" -- a captured>0 move can never be classified as a slip
    regardless of the mate numbers, which is what protects this
    brilliancy from being demoted."""
    candidate = _candidate(
        "queen-takes-bishop-deeper",
        QUEEN_TAKES_BISHOP_MOVES,
        color="black",
        analyzed=True,
        eval_overrides={
            33: {"eval_cp": None, "eval_mate": -6},
            34: {"eval_cp": None, "eval_mate": -5},
        },
    )

    matches = run_scan([candidate], _spec(piece="queen"))

    assert len(matches) == 1
    detail = matches[0].hits[0].detail
    assert "forced the mate home: #6 -> #5" in detail
    assert "gave the queen for a bishop" in detail
    assert "slip" not in detail
    assert run_scan([candidate], _spec(piece="queen", sound_only=True)) != []


# --- run_scan plumbing --------------------------------------------------


def test_run_scan_empty_candidates_returns_empty() -> None:
    assert run_scan([], _spec(piece="minor")) == []


def test_run_scan_carries_the_game_summary_on_a_match() -> None:
    candidate = _candidate("legal", LEGALS_MATE_MOVES, color="white", result="win")

    matches = run_scan([candidate], _spec(piece="queen"))

    assert matches[0].game == candidate.summary


def test_malformed_san_stops_that_games_replay_but_keeps_no_prior_offer() -> None:
    """The highlights tolerance (docs/06-coach.md): a malformed
    continuation stops the walk rather than raising."""
    candidate = _candidate("corrupt", LEGALS_MATE_MOVES[:8])
    corrupted = candidate.model_copy(
        update={"san_moves": [*candidate.san_moves, "Zz9"]}
    )

    # No exception; the malformed ply is simply never reached as a
    # candidate offering move (only genuine SAN plies are).
    assert run_scan([corrupted], _spec(piece="queen")) == []


def test_unsupported_event_raises_a_typed_error() -> None:
    """Every `ScanEventName` has a registered detector by the end of
    slice 2, so a well-formed `ScanEventSpec` can no longer reach
    `UnsupportedScanEventError` -- `model_construct` bypasses pydantic's
    own Literal validation to exercise the defensive backstop anyway
    (docs/06-coach.md build plan)."""
    candidate = _candidate("legal", LEGALS_MATE_MOVES)
    bogus_step = ScanEventSpec.model_construct(event="king_hunt")
    spec = ScanSpec.model_construct(match=[bogus_step])

    with pytest.raises(scan_module.UnsupportedScanEventError):
        run_scan([candidate], spec)


# ======================================================================
# Slice 2: delivered_mate, castled, eval_swing, comeback, chain matcher
# ======================================================================

# White castles kingside at ply 15, then plays the same Greek-gift
# bishop sacrifice test_coach_highlights.py::BXH7_SAC_MOVES verifies:
# 17.Bxh7+ captures a pawn defended only by the king, netting 2. Reused
# here (not imported -- the two test files stay independent) to
# demonstrate the "castled, then sacrificed" chain shape without a
# second round of manual SEE verification.
CASTLE_THEN_SAC_MOVES = [
    "e4", "e6", "d4", "d5", "Nc3", "Nf6", "Bd3", "dxe4", "Nxe4", "Nbd7",
    "Nxf6+", "Nxf6", "Nf3", "Be7", "O-O", "O-O", "Bxh7+",
]  # fmt: skip

# White castles queenside at ply 9 -- no sacrifice follows; used only to
# unit-test `castled`'s own side filter.
CASTLE_QUEENSIDE_MOVES = [
    "d4",
    "d5",
    "Nc3",
    "Nf6",
    "Bf4",
    "Bf5",
    "Qd2",
    "Nc6",
    "O-O-O",
]


def test_castled_kingside_matches_short() -> None:
    candidate = _candidate("castle-short", CASTLE_THEN_SAC_MOVES[:15])
    spec = ScanSpec(match=[ScanEventSpec(event="castled", side="short")])

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 15
    assert hit.san == "O-O"
    assert hit.detail == "castled kingside (short)"


def test_castled_queenside_matches_long_not_short() -> None:
    candidate = _candidate("castle-long", CASTLE_QUEENSIDE_MOVES)

    long_spec = ScanSpec(match=[ScanEventSpec(event="castled", side="long")])
    short_spec = ScanSpec(match=[ScanEventSpec(event="castled", side="short")])
    any_spec = ScanSpec(match=[ScanEventSpec(event="castled", side="any")])

    long_matches = run_scan([candidate], long_spec)
    assert len(long_matches) == 1
    assert long_matches[0].hits[0].ply == 9
    assert long_matches[0].hits[0].detail == "castled queenside (long)"
    assert run_scan([candidate], short_spec) == []
    assert run_scan([candidate], any_spec) != []


def test_castled_no_castling_move_does_not_match() -> None:
    candidate = _candidate("nocastle", FAVOURABLE_CAPTURE_MOVES)
    spec = ScanSpec(match=[ScanEventSpec(event="castled", side="any")])

    assert run_scan([candidate], spec) == []


def test_delivered_mate_matches_at_the_final_ply() -> None:
    candidate = _candidate("mate", LEGALS_MATE_MOVES, color="white", result="win")
    spec = ScanSpec(match=[ScanEventSpec(event="delivered_mate")])

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 13
    assert hit.san == "Nd5#"
    assert hit.detail == "delivered checkmate"


def test_delivered_mate_requires_a_win() -> None:
    """The final position really is checkmate here -- the mate belongs
    to White, so scanning it as Black's own game (a loss) must not
    match (docs/06-coach.md: "result == win AND ... checkmate")."""
    candidate = _candidate("mate-loss", LEGALS_MATE_MOVES, color="black", result="loss")
    spec = ScanSpec(match=[ScanEventSpec(event="delivered_mate")])

    assert run_scan([candidate], spec) == []


def test_delivered_mate_requires_actual_checkmate() -> None:
    candidate = _candidate(
        "notmate", FAVOURABLE_CAPTURE_MOVES, color="white", result="win"
    )
    spec = ScanSpec(match=[ScanEventSpec(event="delivered_mate")])

    assert run_scan([candidate], spec) == []


# --- eval_swing ----------------------------------------------------------

_SWING_MOVES = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]


def test_eval_swing_gained_matches_at_the_default_threshold() -> None:
    candidate = _candidate(
        "swing-gain",
        _SWING_MOVES,
        analyzed=True,
        eval_overrides={3: {"eval_cp": 0}, 4: {"eval_cp": 350}},
    )
    spec = ScanSpec(match=[ScanEventSpec(event="eval_swing", direction="gained")])

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 4
    assert "eval swing +3.50 pawns" in hit.detail
    assert "+0.00 -> +3.50" in hit.detail


def test_eval_swing_lost_matches_a_drop_for_the_player() -> None:
    candidate = _candidate(
        "swing-loss",
        _SWING_MOVES,
        analyzed=True,
        # plies 5 and 6 hold at the same level as ply 4 -- otherwise the
        # baseline-0 default at a later ply would itself read as a
        # rebound "gained" swing, which is not what this test isolates.
        eval_overrides={
            3: {"eval_cp": 0},
            4: {"eval_cp": -350},
            5: {"eval_cp": -350},
            6: {"eval_cp": -350},
        },
    )
    spec = ScanSpec(match=[ScanEventSpec(event="eval_swing", direction="lost")])

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    assert matches[0].hits[0].ply == 4
    # A drop this large in the "gained" direction must not also match.
    gained_spec = ScanSpec(
        match=[ScanEventSpec(event="eval_swing", direction="gained")]
    )
    assert run_scan([candidate], gained_spec) == []


def test_eval_swing_first_move_counts_as_equal() -> None:
    candidate = _candidate(
        "swing-first",
        _SWING_MOVES,
        analyzed=True,
        eval_overrides={1: {"eval_cp": 350}},
    )
    spec = ScanSpec(match=[ScanEventSpec(event="eval_swing", direction="gained")])

    matches = run_scan([candidate], spec)

    assert matches[0].hits[0].ply == 1
    assert "+0.00 -> +3.50" in matches[0].hits[0].detail


def test_eval_swing_respects_custom_threshold() -> None:
    candidate = _candidate(
        "swing-small",
        _SWING_MOVES,
        analyzed=True,
        eval_overrides={3: {"eval_cp": 0}, 4: {"eval_cp": 150}},
    )
    default_spec = ScanSpec(
        match=[ScanEventSpec(event="eval_swing", direction="gained")]
    )
    loose_spec = ScanSpec(
        match=[
            ScanEventSpec(event="eval_swing", direction="gained", min_swing_pawns=1.0)
        ]
    )

    assert run_scan([candidate], default_spec) == []
    assert run_scan([candidate], loose_spec) != []


def test_eval_swing_skips_unanalyzed_candidates() -> None:
    candidate = _candidate("swing-unanalyzed", _SWING_MOVES, analyzed=False)
    spec = ScanSpec(match=[ScanEventSpec(event="eval_swing", direction="gained")])

    assert run_scan([candidate], spec) == []


# --- comeback --------------------------------------------------------------

_COMEBACK_MOVES = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6"]


def test_comeback_matches_the_worst_ply_in_a_win() -> None:
    candidate = _candidate(
        "comeback",
        _COMEBACK_MOVES,
        result="win",
        analyzed=True,
        eval_overrides={
            3: {"eval_cp": -150},
            5: {"eval_cp": -440},  # the worst point
            7: {"eval_cp": -320},
        },
    )
    spec = ScanSpec(match=[ScanEventSpec(event="comeback")])

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    [hit] = matches[0].hits
    assert hit.ply == 5
    assert "-4.40" in hit.detail


def test_comeback_requires_a_win() -> None:
    candidate = _candidate(
        "comeback-loss",
        _COMEBACK_MOVES,
        result="loss",
        analyzed=True,
        eval_overrides={5: {"eval_cp": -440}},
    )
    spec = ScanSpec(match=[ScanEventSpec(event="comeback")])

    assert run_scan([candidate], spec) == []


def test_comeback_requires_crossing_the_threshold() -> None:
    candidate = _candidate(
        "comeback-shallow",
        _COMEBACK_MOVES,
        result="win",
        analyzed=True,
        eval_overrides={5: {"eval_cp": -150}},
    )
    spec = ScanSpec(match=[ScanEventSpec(event="comeback")])

    assert run_scan([candidate], spec) == []


def test_comeback_skips_unanalyzed_candidates() -> None:
    candidate = _candidate("comeback-unanalyzed", _COMEBACK_MOVES, result="win")
    spec = ScanSpec(match=[ScanEventSpec(event="comeback")])

    assert run_scan([candidate], spec) == []


# --- chain matcher -----------------------------------------------------


def test_chain_sacrifice_then_delivered_mate_matches_in_order() -> None:
    """The spike's flagship chain: `sacrifice(queen)` then
    `delivered_mate` within 12 plies, on the same shape the spike
    measured (docs/spike-reports/coach-game-search-events.md)."""
    candidate = _candidate("chain-mate", LEGALS_MATE_MOVES, result="win")
    spec = ScanSpec(
        match=[
            ScanEventSpec(event="sacrifice", piece="queen"),
            ScanEventSpec(event="delivered_mate", within_plies=12),
        ]
    )

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    assert [hit.ply for hit in matches[0].hits] == [9, 13]


def test_chain_respects_within_plies() -> None:
    candidate = _candidate("chain-tight", LEGALS_MATE_MOVES, result="win")
    spec = ScanSpec(
        match=[
            ScanEventSpec(event="sacrifice", piece="queen"),
            ScanEventSpec(event="delivered_mate", within_plies=2),
        ]
    )

    assert run_scan([candidate], spec) == []


def test_chain_never_matches_out_of_order() -> None:
    """A chain is an ORDERED sequence: asking for delivered_mate before
    the sacrifice must not match just because both events occur
    somewhere in the game."""
    candidate = _candidate("chain-order", LEGALS_MATE_MOVES, result="win")
    spec = ScanSpec(
        match=[
            ScanEventSpec(event="delivered_mate"),
            ScanEventSpec(event="sacrifice", piece="queen"),
        ]
    )

    assert run_scan([candidate], spec) == []


def test_chain_castled_then_sacrifice_matches_in_order() -> None:
    """The "castled, then sacrificed" chain shape from the design doc
    (docs/archive/coach-game-search.md), demonstrated on
    the kingside castle + Greek-gift shape rather than the spike's own
    (unavailable) archive game -- the chain mechanism is identical
    either side."""
    candidate = _candidate("chain-castle", CASTLE_THEN_SAC_MOVES, result="win")
    spec = ScanSpec(
        match=[
            ScanEventSpec(event="castled", side="short"),
            ScanEventSpec(event="sacrifice", piece="minor"),
        ]
    )

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    assert [hit.ply for hit in matches[0].hits] == [15, 17]


def test_chain_within_plies_means_nothing_on_the_first_step() -> None:
    """`within_plies` on the first `ScanEventSpec` is documented as a
    no-op (domain.py: "means nothing on the first step") -- setting it
    must not change whether the chain matches."""
    candidate = _candidate("chain-first-within", LEGALS_MATE_MOVES, result="win")
    spec = ScanSpec(
        match=[
            ScanEventSpec(event="sacrifice", piece="queen", within_plies=1),
            ScanEventSpec(event="delivered_mate", within_plies=12),
        ]
    )

    matches = run_scan([candidate], spec)

    assert len(matches) == 1
    assert [hit.ply for hit in matches[0].hits] == [9, 13]
