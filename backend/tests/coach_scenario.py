"""The player behind the coach snapshot test.

One fixed cast of analyzed games, shared by the prompt snapshot and the
regression tests around it. It is modelled on the real 515-game report
that prompted docs/archive/coach-report-improvements.md: someone who opens
1.d4 as White and answers 1.e4 with the Pirc, and whose opponents keep
meeting the d4 with the Englund Gambit. It is built so a fix shows up
as a readable diff of `testdata/coach_prompt.md`:

- the **Englund** is played *at* this student, never by them (finding
  1). It has enough games to clear the sample floor and be named, so
  the prompt has to get the attribution right rather than dodge it by
  burying the line in the long tail. It is `faced` (named by the
  opponent's own move) and above the floor, so it must appear under
  "What they face as White" and nowhere in "Systems the student chose"
  (docs/archive/fixes-2026-07/03-faced-openings.md).
- the **London** and the **Pirc** are the student's own systems --
  `faced` is False for both -- the Pirc split across two lichess names
  that must roll up into one family (finding 2), beside a two-game Ruy
  that must not (finding 2's sample floor).
- no game reaches an **endgame**, so any aggregate that averages in
  per-game zeros reports a healthy endgame the student never played
  (finding 4).
- losses end by **timeout, resignation and checkmate** in different
  proportions (finding 7) — invisible while ingestion collapsed them
  all to "loss".
- there is a losing blunder in a still-contestable position *and* a
  walk-into-mate in an already-lost one, so turning-point selection
  has to choose between them (finding 6).

Move lists are written as one string and split, so the formatter
leaves them on one line and the opening stays readable as a chess line.
"""

from collections.abc import Sequence

from chess_coach.domain import AnalyzedGame, Color, Opening, Result, TimeClass
from tests.factories import make_analyzed

# ply=3: White's own 2.Bf4, the early bishop development that makes this
# "Accelerated" rather than plain London -- the student's own move, so
# `faced` must resolve False (chosen), matching "the student's own
# systems" below.
LONDON = Opening(eco="D02", name="Queen's Pawn Game: Accelerated London System", ply=3)
# ply=4: Black's 2...Nc6, the move that (with the earlier ...e5) fixes
# this as the Englund proper rather than another line in the complex --
# the opponent's move, so `faced` must resolve True (docs/06-coach.md).
ENGLUND = Opening(eco="A40", name="Englund Gambit Complex: Englund Gambit", ply=4)
PIRC = Opening(eco="B07", name="Pirc Defense: Classical Variation", ply=8)
PIRC_AUSTRIAN = Opening(eco="B09", name="Pirc Defense: Austrian Attack", ply=8)
RUY_OPEN = Opening(eco="C80", name="Ruy Lopez: Open Variation", ply=10)

# The student's own systems run past ply 20 into a middlegame, so the
# opening-phase and whole-game ACPL columns measure different things —
# a fixture of nothing but 16-ply miniatures would make the two columns
# identical and quietly stop testing finding 3 at all.
_LONDON = (
    "d4 d5 Bf4 Nf6 e3 e6 Nf3 Bd6 Bg3 O-O Bd3 c5 c3 Nc6 Nbd2 b6 "
    "O-O Bb7 Ne5 Rc8 f4 Ne7 Qf3 Ng6 Nxg6 hxg6 Rae1 Qe7 h3 Rfd8"
)
_ENGLUND = "d4 e5 dxe5 Nc6 Nf3 Qe7 Bf4 Qb4+ Bd2 Qxb2 Bc3 Bb4 Qd2 Bxc3 Qxc3 Qc1+"
_PIRC_CLASSICAL = (
    "e4 d6 d4 Nf6 Nc3 g6 Nf3 Bg7 Be2 O-O O-O Bg4 Be3 Nc6 Qd2 e5 "
    "d5 Ne7 Rad1 Bd7 Ne1 Ne8 f3 f5 Nd3 Nf6 b3 Kh8 a4 fxe4"
)
_PIRC_AUSTRIAN = "e4 d6 d4 Nf6 Nc3 g6 f4 Bg7 Nf3 O-O Bd3 Na6 O-O c5 d5 Bg4"
_RUY_OPEN = (
    "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Nxe4 d4 b5 Bb3 d5 dxe5 Be6 "
    "c3 Bc5 Nbd2 O-O Bc2 Bf5 Nb3 Bg4"
)

# Loss profiles, one entry per player move, padded with zeros when a
# game outlasts them. Reused across games so the fixture varies without
# nineteen hand-tuned lists. The 15-move profiles carry losses on both
# sides of ply 20, which is what makes the two ACPL columns diverge.
_CLEAN = (0, 0, 0, 12, 0, 35, 0, 18, 0, 0, 25, 0, 40, 0, 15)
_SLOPPY = (0, 0, 25, 60, 0, 110, 0, 30, 0, 45, 0, 240, 0, 20, 55)
_SHARP = (0, 0, 0, 45, 0, 20, 0, 55, 0, 0, 0, 30, 0, 90, 0)
# A walk into mate, played when the position was already lost.
_MATED = (0, 0, 40, 0, 0, 0, 130, 9_950)
# The instructive one: a level position thrown away in the middlegame.
_COLLAPSE = (0, 0, 0, 0, 0, 24, 0, 60, 310, 40, 0, 95)

_DAY = 86_400
_JUNE_15 = 1_781_000_000  # the newest game; the prompt should cite dates

# (result, termination, loss profile, rating, days before the newest game)
_Spec = tuple[Result, str, Sequence[int], int, int]

_LONDON_GAMES: list[_Spec] = [
    ("win", "win", _CLEAN, 1455, 74),
    ("win", "win", _CLEAN, 1462, 61),
    ("loss", "resigned", _SLOPPY, 1470, 47),
    ("win", "win", _SHARP, 1478, 33),
    ("draw", "agreed", _CLEAN, 1489, 19),
    ("loss", "timeout", _SLOPPY, 1496, 5),
]

_ENGLUND_GAMES: list[_Spec] = [
    ("loss", "checkmated", _MATED, 1451, 70),
    ("win", "win", _SHARP, 1458, 55),
    ("loss", "timeout", _SLOPPY, 1466, 40),
    ("loss", "resigned", _MATED, 1474, 26),
    ("win", "win", _CLEAN, 1487, 11),
]

_PIRC_GAMES: list[_Spec] = [
    ("win", "win", _CLEAN, 1560, 72),
    ("loss", "resigned", _SLOPPY, 1552, 58),
    ("draw", "repetition", _SHARP, 1571, 44),
    ("win", "win", _CLEAN, 1584, 29),
]
_PIRC_AUSTRIAN_GAMES: list[_Spec] = [
    ("loss", "timeout", _SLOPPY, 1576, 15),
    ("win", "win", _SHARP, 1598, 2),
]

_RUY_GAMES: list[_Spec] = [
    ("win", "win", _CLEAN[:12], 1468, 51),
    ("loss", "resigned", _COLLAPSE, 1483, 8),
]


# Opponent rating relative to the student's, cycled across each series.
# The spread straddles the stronger/similar/weaker bands on purpose: an
# opposition split where every game lands in one band reports "n/a" for
# the other two and tests nothing.
_OPPONENT_DELTAS = (-165, 40, 205, -35, 120, -90)


def _series(
    prefix: str,
    line: str,
    *,
    color: Color,
    opening: Opening,
    time_class: TimeClass,
    specs: list[_Spec],
) -> list[AnalyzedGame]:
    return [
        make_analyzed(
            f"{prefix}-{index + 1}",
            line.split(),
            color=color,
            result=result,
            opening=opening,
            losses=losses,
            time_class=time_class,
            end_time=_JUNE_15 - days_ago * _DAY,
            player_rating=rating,
            opponent_rating=rating + _OPPONENT_DELTAS[index % len(_OPPONENT_DELTAS)],
            termination=termination,
        )
        for index, (result, termination, losses, rating, days_ago) in enumerate(specs)
    ]


def scenario_games() -> list[AnalyzedGame]:
    """Nineteen analyzed games, newest first as storage returns them."""
    games = [
        # The student's own White system.
        *_series(
            "g-london",
            _LONDON,
            color="white",
            opening=LONDON,
            time_class="blitz",
            specs=_LONDON_GAMES,
        ),
        # The opponent's gambit, met as White. The student never chose
        # this and must never be told to stop playing it.
        *_series(
            "g-englund",
            _ENGLUND,
            color="white",
            opening=ENGLUND,
            time_class="blitz",
            specs=_ENGLUND_GAMES,
        ),
        # The student's own Black defense, split across two names that
        # share one system and must roll up together.
        *_series(
            "g-pirc",
            _PIRC_CLASSICAL,
            color="black",
            opening=PIRC,
            time_class="rapid",
            specs=_PIRC_GAMES,
        ),
        *_series(
            "g-pirc-aus",
            _PIRC_AUSTRIAN,
            color="black",
            opening=PIRC_AUSTRIAN,
            time_class="rapid",
            specs=_PIRC_AUSTRIAN_GAMES,
        ),
        # Two games is not a repertoire: this belongs in the long tail.
        *_series(
            "g-ruy",
            _RUY_OPEN,
            color="white",
            opening=RUY_OPEN,
            time_class="bullet",
            specs=_RUY_GAMES,
        ),
    ]
    games.sort(key=lambda game: -game.end_time)
    return games
