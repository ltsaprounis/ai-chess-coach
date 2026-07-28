"""Coach component tests (docs/06-coach.md)."""

import asyncio
import os
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from copilot import ToolSet
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionEvent,
    SessionEventType,
    SessionIdleData,
)
from copilot.tools import Tool, ToolInvocation, ToolResult

import chess_coach.coach.providers as providers_module
from chess_coach.coach import (
    ClaudeAgentSdkProvider,
    CoachProvider,
    CoachProviderError,
    CopilotSdkProvider,
    ExplainEvent,
    MoveContext,
    append_game_links,
    build_move_context,
    build_report,
    create_provider,
    render_explain_prompt,
    render_prompt,
)
from chess_coach.domain import (
    AnalyzedGame,
    ErrorPattern,
    EvalLine,
    LlmConfig,
    MoveEval,
    Opening,
    Phase,
    PlayerReport,
    Record,
    TimeClass,
)
from tests.coach_scenario import scenario_games
from tests.factories import make_analysis, make_analyzed, make_game

RUY = Opening(eco="C60", name="Ruy Lopez", ply=5)
PROMPT_SNAPSHOT = Path(__file__).parent / "testdata" / "coach_prompt.md"


def test_build_report_aggregates_player_stats() -> None:
    game1 = make_analyzed(
        "g-1",
        ["e4", "e5", "Nf3", "Nc6"],
        color="white",
        result="win",
        opening=Opening(eco="C60", name="Ruy Lopez", ply=3),
        losses=[0, 20],  # white's two moves: e4 (0), Nf3 (20)
    )
    game2 = make_analyzed(
        "g-2",
        ["e4", "e5", "Nf3", "Nc6"],
        color="white",
        result="loss",
        opening=None,
        losses=[10, 30],
    )
    report = build_report("testuser", [game1, game2])

    assert report.username == "testuser"
    assert report.games_analyzed == 2
    assert report.player_moves == 4  # two white moves per game
    assert report.record == Record(games=2, wins=1, losses=1, draws=0)
    # move-weighted: (0 + 20 + 10 + 30) / 4 -- never a mean of per-game means.
    assert report.overall_acpl == 15.0
    assert report.phases["opening"].moves == 4
    assert report.phases["opening"].acpl == 15.0
    assert report.phases["endgame"].moves == 0
    assert report.phases["endgame"].acpl is None

    # Only the classified game contributes to the repertoire.
    assert len(report.openings) == 1
    stats = report.openings[0]
    assert stats.eco == "C60"
    assert stats.color == "white"
    assert stats.games == 1
    assert stats.wins == 1
    assert stats.analyzed_games == 1
    assert stats.system == "1.e4 2.Nf3"
    assert stats.first_moves == "1.e4 e5 2.Nf3 Nc6"
    assert stats.opening_acpl == 10.0  # (0 + 20) / 2
    assert stats.avg_cp_loss == 10.0


def test_build_report_records_time_class_filter() -> None:
    report = build_report("testuser", [], time_class="blitz")
    assert report.time_class == "blitz"
    assert build_report("testuser", []).time_class is None


def test_build_report_copies_scope_kwargs_onto_report() -> None:
    """docs/06-coach.md: `requested_since`/`requested_until`/
    `games_in_scope` carry no aggregation logic -- `build_report` copies
    them verbatim onto the report, and all three default to None."""
    report = build_report(
        "testuser",
        [],
        requested_since=1_767_225_600,
        requested_until=1_785_110_400,
        games_in_scope=30,
    )
    assert report.requested_since == 1_767_225_600
    assert report.requested_until == 1_785_110_400
    assert report.games_in_scope == 30

    defaults = build_report("testuser", [])
    assert defaults.requested_since is None
    assert defaults.requested_until is None
    assert defaults.games_in_scope is None


def test_openings_sorted_worst_first() -> None:
    # Impact (games x win-rate deficit), not raw win rate: the 5-game
    # all-loss line must outrank the 2-game all-win line.
    games = [
        make_analyzed(
            f"w{i}",
            ["e4", "e5", "Nf3", "Nc6"],
            color="white",
            result="win",
            opening=Opening(eco="C60", name="Ruy Lopez", ply=3),
        )
        for i in range(2)
    ] + [
        make_analyzed(
            f"l{i}",
            ["d4", "d5", "c4", "e6"],
            color="white",
            result="loss",
            opening=Opening(eco="D06", name="Queen's Gambit", ply=3),
        )
        for i in range(5)
    ]
    report = build_report("testuser", games)
    assert [s.eco for s in report.openings] == ["D06", "C60"]


def test_critical_positions_replay_to_fen() -> None:
    # White's second move (Nf3, index 2 -> ply 3) loses 300 cp while the
    # game is still roughly level (+0.30) -- a genuine equal -> losing
    # crossing, and so a qualifying turning point.
    evals = [
        MoveEval(
            ply=1,
            san="e4",
            eval_cp=30,
            eval_mate=None,
            best_move="e2e4",
            cp_loss=0,
            judgment="best",
        ),
        MoveEval(
            ply=2,
            san="e5",
            eval_cp=30,
            eval_mate=None,
            best_move="e7e5",
            cp_loss=0,
            judgment="best",
        ),
        MoveEval(
            ply=3,
            san="Nf3",
            eval_cp=-270,
            eval_mate=None,
            best_move="d2d4",
            cp_loss=300,
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-crit").model_copy(update={"evals": evals})
    game = make_game(
        id="g-crit",
        san_moves=["e4", "e5", "Nf3"],
        color="white",
        time_class="blitz",
        end_time=1_781_000_000,
    )
    report = build_report(
        "testuser",
        [
            AnalyzedGame.model_validate(
                {**game.model_dump(), "opening": RUY, "analysis": analysis}
            )
        ],
    )

    assert len(report.critical_positions) == 1
    critical = report.critical_positions[0]
    assert critical.fen.startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w")
    assert critical.played == "Nf3"
    assert critical.best == "d4"  # UCI d2d4 rendered as SAN
    assert critical.cp_loss == 300
    assert critical.game_id == "g-crit"
    assert critical.color == "white"
    assert critical.time_class == "blitz"
    assert critical.end_time == 1_781_000_000
    assert critical.opening_name == "Ruy Lopez"
    assert critical.ply == 3
    assert critical.move_number == 2
    assert critical.leading_up == ["e4", "e5"]
    assert critical.eval_before_cp == 30
    assert critical.eval_after_cp == -270


def test_englund_attributed_to_opponent_not_student_repertoire() -> None:
    """The Englund regression (COACH-REPORT-IMPROVEMENTS.md finding 1) and
    its faced/chosen split (docs/fixes-2026-07/03-faced-openings.md).

    The student is White; the Englund is the opponent's own gambit
    (1...e5!? in reply to 1.d4). The guarantee this protects is
    attribution, not naming or rendering: `system` must carry only the
    student's own moves, so the opponent's defining reply -- 1...e5, the
    move that makes this the Englund at all -- can never appear there,
    only in `first_moves`. A revert that built `system` from the full
    line (both sides), or from the wrong side's plies, would surface
    "e5" as a standalone token in `system`; asserting only that the row
    is rendered somewhere would not catch that.

    `faced` must also resolve True from the opponent-named ply, and the
    prompt must render the row under "What you face as White" -- never
    under "Systems you chose", where a misattributed line would invite
    advice to stop playing a gambit the student never chose.
    """
    report = build_report("testuser", scenario_games())
    englund = next(o for o in report.openings if o.eco == "A40")
    assert englund.color == "white"  # the student had White in this game
    assert "Englund" in englund.name  # still correctly classified
    assert englund.faced is True  # named by the opponent's own move

    system_tokens = englund.system.split()
    first_move_tokens = englund.first_moves.split()
    assert "e5" not in system_tokens  # the opponent's move, never the student's
    assert "e5" in first_move_tokens  # ...but visible in the full line
    assert englund.system == "1.d4 2.dxe5 3.Nf3"

    prompt = render_prompt(report)
    white_section = prompt.split("### As White")[1].split("### As Black")[0]
    chosen_part, faced_part = white_section.split("#### What you face as White")
    assert "Englund" not in chosen_part  # never listed as a system the student chose
    assert "Englund" in faced_part  # named -- it clears the 5-game floor
    # The full line -- both the opponent's choice and the student's reply
    # to it -- must still be legible in the faced row.
    assert "1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7" in faced_part


def test_phase_aggregation_ignores_games_that_never_reach_an_endgame() -> None:
    """Finding 4: a phase never reached reads as "no moves", not 0.0 cp
    loss averaged in from every game that stopped short of it."""
    report = build_report("testuser", scenario_games())
    assert report.phases["endgame"].moves == 0
    assert report.phases["endgame"].acpl is None


def test_single_game_lines_land_in_long_tail_not_top_of_table() -> None:
    london = [
        make_analyzed(
            f"g-london-{i}",
            ["d4", "Nf6", "Nf3", "d5", "Bf4"],
            color="white",
            result="win",
            opening=Opening(eco="D02", name="London System", ply=5),
        )
        for i in range(5)
    ]
    bird = make_analyzed(
        "g-bird-solo",
        ["f4", "d5", "Nf3", "Nf6", "g3"],
        color="white",
        result="loss",
        opening=Opening(eco="A02", name="Bird Opening", ply=5),
    )
    report = build_report("testuser", [*london, bird])
    prompt = render_prompt(report)
    white_section = prompt.split("### As White")[1]

    assert "London System" in white_section
    assert "Bird Opening" not in white_section
    assert "Long tail: 1 line" in white_section
    assert "1 game total" in white_section


def test_turning_points_prefer_contestable_blunder_over_walk_into_mate() -> None:
    """Finding 6: a mate-scale loss in an already-lost position must never
    crowd out an instructive blunder in a position still up for grabs."""
    report = build_report("testuser", scenario_games())

    assert report.critical_positions  # a turning point was found at all
    assert all(p.cp_loss < 9_000 for p in report.critical_positions)
    # The Ruy's 310cp middlegame collapse (still-contestable) must be
    # selected -- never a mate-scale loss in an already-lost position,
    # which the assertion above already rules out.
    assert any(p.cp_loss == 310 for p in report.critical_positions)


def test_critical_positions_contestable_band_boundary() -> None:
    """docs/06-coach.md: a turning point must fall inside the roughly
    +/-3 pawn "still contestable" band, measured on the before-eval. A
    blunder just inside the band is eligible; one centipawn past it is
    not, even though both are otherwise identical qualifying blunders
    (a mistake/blunder judgment crossing a decision boundary).
    """
    from chess_coach.coach.report import (
        _CONTESTABLE_BAND,  # pyright: ignore[reportPrivateUsage]
    )

    def _one_turning_point_game(game_id: str, before_cp: int) -> AnalyzedGame:
        evals = [
            MoveEval(
                ply=1, san="e4", eval_cp=30, eval_mate=None,
                best_move="e2e4", cp_loss=0, judgment="best",
            ),
            MoveEval(
                ply=2, san="e5", eval_cp=before_cp, eval_mate=None,
                best_move="e7e5", cp_loss=0, judgment="best",
            ),
            MoveEval(
                ply=3, san="Nf3", eval_cp=before_cp - 400, eval_mate=None,
                best_move="d2d4", cp_loss=300, judgment="blunder",
            ),
        ]  # fmt: skip
        analysis = make_analysis(game_id=game_id).model_copy(update={"evals": evals})
        game = make_game(id=game_id, san_moves=["e4", "e5", "Nf3"], color="white")
        return AnalyzedGame.model_validate(
            {**game.model_dump(), "opening": None, "analysis": analysis}
        )

    # Both swing from "winning" to "equal" (a genuine decision-boundary
    # crossing) with an identical 300cp blunder; they differ only in
    # whether the before-eval sits inside or outside the band.
    inside = _one_turning_point_game("g-inside", before_cp=_CONTESTABLE_BAND)
    outside = _one_turning_point_game("g-outside", before_cp=_CONTESTABLE_BAND + 1)

    assert build_report("testuser", [inside]).critical_positions
    assert not build_report("testuser", [outside]).critical_positions


def test_select_critical_diversity_cap_binds_with_enough_candidates() -> None:
    """`_select_critical`'s per-bucket cap only does anything once there
    are more candidates than `_TOP_CRITICAL` -- the scenario fixture's 8
    candidates never exercise it (docs/06-coach.md's spread rule). Build
    13 directly, cluster 5 of them on one phase so the cap must reject
    one of the game's own most-recent candidates in favour of an older,
    more diverse one, and check the resulting spread.
    """
    from chess_coach.coach.report import (
        _DIVERSITY_CAP_FRACTION,  # pyright: ignore[reportPrivateUsage]
        _TOP_CRITICAL,  # pyright: ignore[reportPrivateUsage]
        _Candidate,  # pyright: ignore[reportPrivateUsage]
        _select_critical,  # pyright: ignore[reportPrivateUsage]
    )

    time_classes: list[TimeClass] = ["bullet", "blitz", "rapid", "daily"]
    # Five middlegame candidates as the five most-recent games -- more
    # than the cap allows from one phase. Candidates 6-13 fill the rest
    # of the table from the other two phases, four apiece, none of them
    # tripping the phase, opening or time-class cap.
    phases: list[Phase] = [
        "middlegame",
        "middlegame",
        "middlegame",
        "middlegame",
        "middlegame",
        "opening",
        "endgame",
        "opening",
        "endgame",
        "opening",
        "endgame",
        "opening",
        "endgame",
    ]
    base_time = 1_800_000_000
    candidates = [
        _Candidate(
            game=make_analyzed(
                f"g-{i}",
                ["e4", "e5"],
                opening=Opening(eco=f"Z{i:02d}", name=f"Test Opening {i}", ply=2),
                time_class=time_classes[(i - 1) % len(time_classes)],
                end_time=base_time - i * 86_400,  # i=1 most recent, i=13 oldest
            ),
            idx=0,
            phase=phase,
        )
        for i, phase in enumerate(phases, start=1)
    ]

    selected = _select_critical(candidates)

    assert len(selected) == _TOP_CRITICAL  # capped down from 13
    selected_ids = {c.game.id for c in selected}
    # The 5th-most-recent candidate ("g-5") is the one over the phase cap
    # -- it must lose out to "g-13", the *oldest* candidate, which the
    # cap-free fill loop only reaches because g-5's slot never opens up.
    assert "g-5" not in selected_ids
    assert "g-13" in selected_ids

    # The spread the cap exists to produce: every phase and time class
    # appears, and none of them dominates the final 12.
    cap = round(_TOP_CRITICAL * _DIVERSITY_CAP_FRACTION)
    phase_counts: dict[str, int] = defaultdict(int)
    time_class_counts: dict[str, int] = defaultdict(int)
    for c in selected:
        phase_counts[c.phase] += 1
        time_class_counts[c.game.time_class] += 1
    assert set(phase_counts) == {"middlegame", "opening", "endgame"}
    assert all(count <= cap for count in phase_counts.values())
    assert set(time_class_counts) == set(time_classes)
    assert all(count <= cap for count in time_class_counts.values())


# --- error pattern tags (docs/06-coach.md) ---------------------------------


def test_error_pattern_hangs_piece() -> None:
    # 3.Qxe5+?? Nxe5: the queen is undefended on e5 and simply recaptured,
    # with no check involved.
    game = make_analyzed(
        "g-hangs",
        ["e4", "e5", "Qh5", "Nc6", "Qxe5+", "Nxe5"],
        color="white",
        result="loss",
        losses=[0, 0, 700],
    )
    report = build_report("testuser", [game])
    patterns = {p.pattern: p for p in report.error_patterns}
    assert "hangs_piece" in patterns
    assert patterns["hangs_piece"].count == 1
    assert patterns["hangs_piece"].label == "Hung a piece"


def test_error_pattern_hangs_piece_to_check() -> None:
    # 4.b4?? Bxb4+: the pawn is undefended and the recapture opens with
    # check along the a5-e1 diagonal.
    game = make_analyzed(
        "g-hangs-check",
        ["e4", "e5", "d4", "exd4", "Bc4", "Bc5", "b4", "Bxb4+"],
        color="white",
        result="loss",
        losses=[0, 0, 0, 250],
    )
    report = build_report("testuser", [game])
    patterns = {p.pattern: p for p in report.error_patterns}
    assert "hangs_piece_to_check" in patterns
    assert patterns["hangs_piece_to_check"].count == 1


def test_error_pattern_back_rank() -> None:
    # The long dark-square diagonal is opened (b3, Na3, h3) and the
    # bishop swoops all the way down to capture the undefended rook on
    # the back rank.
    game = make_analyzed(
        "g-back-rank",
        ["b3", "g6", "Na3", "Bg7", "h3", "Bxa1"],
        color="white",
        result="loss",
        losses=[0, 0, 550],
    )
    report = build_report("testuser", [game])
    patterns = {p.pattern: p for p in report.error_patterns}
    assert "back_rank" in patterns
    assert patterns["back_rank"].count == 1


def test_error_pattern_missed_win() -> None:
    game = make_game(id="g-missed-win", san_moves=["e4", "e5", "Nf3"], color="white")
    evals = [
        MoveEval(
            ply=1, san="e4", eval_cp=350, eval_mate=None,
            best_move="e2e4", cp_loss=0, judgment="best",
        ),
        MoveEval(
            ply=2, san="e5", eval_cp=350, eval_mate=None,
            best_move="e7e5", cp_loss=0, judgment="best",
        ),
        MoveEval(
            ply=3, san="Nf3", eval_cp=50, eval_mate=None,
            best_move="g1f3", cp_loss=300, judgment="blunder",
        ),
    ]  # fmt: skip
    analysis = make_analysis(game_id="g-missed-win").model_copy(update={"evals": evals})
    analyzed_game = AnalyzedGame.model_validate(
        {**game.model_dump(), "opening": None, "analysis": analysis}
    )
    report = build_report("testuser", [analyzed_game])
    patterns = {p.pattern: p for p in report.error_patterns}
    assert "missed_win" in patterns
    assert patterns["missed_win"].count == 1
    assert patterns["missed_win"].label == "Let a winning position slip"


def test_error_pattern_walks_into_mate() -> None:
    game = make_analyzed(
        "g-mate",
        ["f3", "e5", "g4", "Qh4#"],
        color="white",
        result="loss",
        losses=[0, 9_950],
    )
    report = build_report("testuser", [game])
    patterns = {p.pattern: p for p in report.error_patterns}
    assert "walks_into_mate" in patterns
    assert patterns["walks_into_mate"].count == 1


def test_render_prompt_matches_snapshot() -> None:
    """The prompt is a user-visible artifact — changes must be reviewed.

    docs/06-coach.md calls for a snapshot test precisely because
    determinism is not stability: the template can change completely
    and still render the same string twice. Regenerate deliberately
    with `UPDATE_SNAPSHOTS=1 uv run pytest -k snapshot`, then read the
    diff of `testdata/coach_prompt.md` as the review artifact.

    Scope info is passed (a requested window plus a `games_in_scope`
    larger than the scenario's 19 games) so the snapshot exercises the
    coverage path -- the "N of M" line and the partial-coverage caveat
    (docs/06-coach.md, "Coverage is stated, not implied") -- rather than
    the scope-free rendering already covered by
    test_scope_free_prompt_renders_with_no_coverage_lines.
    """
    report = build_report(
        "testuser",
        scenario_games(),
        requested_since=1_767_225_600,  # 2026-01-01
        requested_until=1_785_110_400,  # 2026-07-27
        games_in_scope=30,  # > 19 analyzed, so the caveat renders
    )
    prompt = render_prompt(report)

    assert prompt == render_prompt(report), "render_prompt is not deterministic"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        PROMPT_SNAPSHOT.write_text(prompt)
    assert prompt == PROMPT_SNAPSHOT.read_text()


def test_mate_scale_losses_render_as_words_not_centipawns() -> None:
    # A mate-scale loss must never appear as a raw centipawn number
    # anywhere in the prompt -- it surfaces as the walks_into_mate error
    # pattern (never as a critical position -- those exclude mate-scale
    # losses outright, see test_turning_points_prefer_contestable_...).
    game = make_analyzed(
        "g-mate",
        ["f3", "e5", "g4", "Qh4#"],
        color="white",
        result="loss",
        losses=[0, 9_950],
    )
    report = build_report("testuser", [game])
    prompt = render_prompt(report)

    assert "9950" not in prompt
    assert "10050" not in prompt
    assert "Walked into a forced mate" in prompt


def test_empty_report_prompt_has_no_empty_sections() -> None:
    prompt = render_prompt(build_report("testuser", []))
    assert "Repertoire" not in prompt
    assert "Turning points" not in prompt
    assert "Trend" not in prompt
    assert "How games end" not in prompt
    assert "Recurring error patterns" not in prompt


# --- scope/coverage rendering (docs/06-coach.md, "Coverage is stated,
# --- not implied") ----------------------------------------------------


def test_scope_free_prompt_renders_with_no_coverage_lines() -> None:
    """With `requested_since`/`requested_until`/`games_in_scope` all None
    (the default), the section must render exactly as it always did --
    the hard backward-compatibility requirement docs/06-coach.md states.
    """
    report = build_report("testuser", scenario_games())
    prompt = render_prompt(report)

    assert "Requested:" not in prompt
    assert "Coverage:" not in prompt
    assert "not engine-analyzed" not in prompt


def test_full_coverage_renders_n_of_n_with_no_caveat() -> None:
    report = build_report(
        "testuser",
        scenario_games(),
        games_in_scope=19,  # == games_analyzed
    )
    prompt = render_prompt(report)

    assert "Coverage: 19 of 19 games in scope are analyzed" in prompt
    assert "not engine-analyzed" not in prompt


def test_partial_coverage_renders_caveat() -> None:
    report = build_report("testuser", scenario_games(), games_in_scope=25)
    prompt = render_prompt(report)

    assert "Coverage: 19 of 25 games in scope are analyzed" in prompt
    assert (
        "Note: the other 6 games in scope are not engine-analyzed; every "
        "figure below describes only the analyzed span." in prompt
    )


def test_requested_window_handles_one_sided_bounds() -> None:
    since_only = render_prompt(
        build_report("testuser", scenario_games(), requested_since=1_767_225_600)
    )
    assert "- Requested: since 2026-01-01" in since_only

    until_only = render_prompt(
        build_report("testuser", scenario_games(), requested_until=1_785_110_400)
    )
    assert "- Requested: until 2026-07-27" in until_only

    both = render_prompt(
        build_report(
            "testuser",
            scenario_games(),
            requested_since=1_767_225_600,
            requested_until=1_785_110_400,
        )
    )
    assert "- Requested: 2026-01-01 to 2026-07-27" in both


def test_turning_point_entries_carry_their_fen() -> None:
    """The FEN is what makes `analyze_position` usable from the prompt at
    all (docs/06-coach.md) -- every rendered turning point must carry
    its own entry's FEN, backticked, on its own line."""
    report = build_report("testuser", scenario_games())
    prompt = render_prompt(report)

    assert report.critical_positions  # the fixture produces turning points
    for position in report.critical_positions:
        assert f"FEN: `{position.fen}`" in prompt


# --- game links (docs/06-coach.md, "Game links") ---------------------------


def test_instructions_contain_new_citation_rule() -> None:
    prompt = render_prompt(build_report("testuser", scenario_games()))
    assert (
        "written as a markdown reference link through the entry's `cite` "
        "handle" in prompt
    )
    assert "never an invented handle" in prompt


def test_turning_point_and_error_example_handles_assigned_in_order() -> None:
    """docs/06-coach.md: handles are assigned `g1`, `g2`, ... in render
    order over distinct `(game_id, ply)` targets -- turning points first,
    then error-pattern examples -- and the numbering is stable across
    renders."""
    report = build_report("testuser", scenario_games())
    prompt = render_prompt(report)

    assert prompt == render_prompt(report)  # stable numbering

    turning_points_section = prompt.split("## Turning points")[1].split(
        "## Instructions"
    )[0]
    for n in range(1, len(report.critical_positions) + 1):
        assert f"-- cite [g{n}]" in turning_points_section

    # The fixture's two error patterns (Back-rank vulnerability, Walked
    # into a forced mate) share one example position distinct from every
    # turning point (tests/coach_scenario.py) -- both rows must cite one
    # handle freshly minted after the last turning-point handle, not two
    # different ones.
    shared_handle = f"g{len(report.critical_positions) + 1}"
    error_section = prompt.split("## Recurring error patterns")[1].split(
        "## Turning points"
    )[0]
    assert error_section.count(f"(cite [{shared_handle}])") == 2


def test_error_example_reuses_turning_point_handle_for_shared_position() -> None:
    """docs/06-coach.md: an error-pattern example landing on the exact
    `(game_id, ply)` a turning point already names must reuse that turning
    point's handle instead of minting a second one for the same
    position."""
    report = build_report("testuser", scenario_games())
    turning_point = report.critical_positions[0]  # renders as "### 1. ... [g1]"
    shared_example = ErrorPattern(
        pattern="hangs_piece",
        label="Hung a piece",
        count=1,
        share_of_blunders=1.0,
        example_game_id=turning_point.game_id,
        example_ply=turning_point.ply,
        example_end_time=turning_point.end_time,
        example_move_number=turning_point.move_number,
    )
    report = report.model_copy(update={"error_patterns": [shared_example]})
    prompt = render_prompt(report)

    error_section = prompt.split("## Recurring error patterns")[1].split(
        "## Turning points"
    )[0]
    assert "(cite [g1])" in error_section
    assert error_section.count("cite [") == 1  # reused, not a fresh handle


def test_error_example_without_position_renders_no_handle() -> None:
    """The "n/a" path -- an error pattern with no example fields -- must
    render without a cite handle; there is nothing to cite."""
    report = build_report("testuser", scenario_games())
    no_example = ErrorPattern(
        pattern="missed_win",
        label="Let a winning position slip",
        count=3,
        share_of_blunders=0.5,
    )
    report = report.model_copy(update={"error_patterns": [no_example]})
    prompt = render_prompt(report)

    error_section = prompt.split("## Recurring error patterns")[1].split(
        "## Turning points"
    )[0]
    assert "| n/a |" in error_section
    assert "cite [" not in error_section


def _two_link_report() -> PlayerReport:
    """A minimal report with exactly two citable positions -- handles `g1`
    and `g2` -- for `append_game_links` tests that need known, stable
    handles rather than the full 9-handle scenario fixture."""
    report = build_report("testuser", scenario_games())
    return report.model_copy(
        update={
            "critical_positions": report.critical_positions[:2],
            "error_patterns": [],
        }
    )


def test_append_game_links_appends_correct_definitions() -> None:
    report = _two_link_report()
    first, second = report.critical_positions
    advice = "See [your move][g1] and [the other one][g2]."

    result = append_game_links(advice, report)

    assert result == (
        f"{advice}\n\n"
        f"[g1]: /games/{first.game_id}?ply={first.ply}\n"
        f"[g2]: /games/{second.game_id}?ply={second.ply}"
    )


def test_append_game_links_normalizes_inline_slip() -> None:
    """`[text](gN)` -- the model reaching for inline markdown syntax
    instead of the reference form the instructions ask for -- normalizes
    to reference style so it resolves through the appended definition."""
    report = _two_link_report()
    first = report.critical_positions[0]
    advice = "Check [your blunder](g1) here."

    result = append_game_links(advice, report)

    assert result.startswith("Check [your blunder][g1] here.\n\n")
    assert f"[g1]: /games/{first.game_id}?ply={first.ply}" in result


def test_append_game_links_strips_unoffered_handle_to_plain_text() -> None:
    """An invented handle -- one the prompt never offered -- is exactly
    as unfindable as no citation at all, so it degrades to plain text
    rather than resolving to nothing or crashing."""
    report = _two_link_report()
    advice = "This cites [a bogus game][g9] that was never offered."

    result = append_game_links(advice, report)

    assert "[a bogus game][g9]" not in result
    assert result.startswith("This cites a bogus game that was never offered.\n\n")


def test_append_game_links_degrades_unoffered_inline_slip_too() -> None:
    """An inline-style citation `[text](gN)` through an unoffered handle
    must degrade exactly like a reference-style one -- not survive as a
    live anchor with a broken "gN" href (docs/06-coach.md: "an invented
    handle renders as its text, in inline or reference form alike")."""
    report = _two_link_report()
    advice = "This cites [a bogus game](g9) that was never offered."

    result = append_game_links(advice, report)

    assert "(g9)" not in result
    assert "[a bogus game]" not in result
    assert result.startswith("This cites a bogus game that was never offered.\n\n")


def test_append_game_links_leaves_non_handle_reference_links_alone() -> None:
    report = _two_link_report()
    advice = "See [the docs][docs-ref] for background."

    result = append_game_links(advice, report)

    assert result.startswith(advice)
    assert "[the docs][docs-ref]" in result


def test_append_game_links_strips_model_authored_definition_hijack() -> None:
    """CommonMark resolves a repeated reference definition to the *first*
    one in the document, so a model-authored `[gN]: ...` line would win
    against the minted definition appended below and could point a
    handle anywhere -- it must be stripped, leaving only the minted one
    standing, even though the citation through that same handle is
    legitimate and must still resolve."""
    report = _two_link_report()
    first, second = report.critical_positions
    advice = (
        "See [your move][g1] for the idea.\n"
        "[g1]: https://evil.example/x\n"
        "Keep training."
    )

    result = append_game_links(advice, report)

    assert "https://evil.example/x" not in result
    assert "See [your move][g1] for the idea." in result
    assert "Keep training." in result
    assert result.count(f"[g1]: /games/{first.game_id}?ply={first.ply}") == 1
    assert f"[g2]: /games/{second.game_id}?ply={second.ply}" in result


def test_append_game_links_strips_definition_hijack_even_for_unoffered_handle() -> None:
    """The stripping rule applies unconditionally -- a model-authored
    definition line for a handle the prompt never even offered is still
    a hijack attempt against whatever a later, legitimate run might mint,
    so it is stripped regardless."""
    report = _two_link_report()
    advice = "Ignore this.\n[g9]: https://evil.example/y\nMore text."

    result = append_game_links(advice, report)

    assert "https://evil.example/y" not in result
    assert "[g9]:" not in result


def test_append_game_links_degrades_but_appends_nothing_with_no_citable_games() -> None:
    """With no citable games every handle is "unknown", so the normalize
    and degrade passes still run -- an invented citation degrades to
    plain text -- but there are no offered handles, so nothing is
    appended."""
    report = build_report("testuser", [])  # no games -> nothing citable
    advice = "Keep grinding tactics. [stray][g1] link here too."

    result = append_game_links(advice, report)

    assert result == "Keep grinding tactics. stray link here too."


async def test_agent_sdk_provider_collects_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["model"] = options.model
        captured["system_prompt"] = options.system_prompt

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Work on your endgames.")],
                model="claude-opus-4-8",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    assert captured["prompt"] == "coach me"
    assert captured["model"] == "claude-opus-4-8"
    # The coach persona must replace Claude Code's coding persona.
    system_prompt = captured["system_prompt"]
    assert isinstance(system_prompt, str) and "chess coach" in system_prompt


async def test_agent_sdk_provider_surfaces_error_detail_from_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",  # the SDK really does this on auth errors
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=0,
                session_id="s",
                result="Not logged in · Please run /login",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="Not logged in"):
        await provider.complete("coach me")


async def test_agent_sdk_provider_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        raise FileNotFoundError("claude binary not found")

    monkeypatch.setattr(providers_module, "query", broken_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        await provider.complete("coach me")


async def test_agent_sdk_provider_complete_with_analyst_runs_agentically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/fixes-2026-07/04-report-engine-tool.md: given an analyst,
    complete() reuses explain()'s MCP-wrapped tool mechanics under the
    report turn budget, and text across the whole tool loop -- before and
    after an engine call -- concatenates into the returned advice.
    """
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Let's verify the critical line.")],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1",
                        name="mcp__engine__analyze_position",
                        input={"fen": "fen-after"},
                    )
                ],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[TextBlock(text=" Confirmed: Nxe5 wins the exchange.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=2,
                session_id="s",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    advice = await provider.complete("write the report", stub_analyst)

    assert (
        advice == "Let's verify the critical line. Confirmed: Nxe5 wins the exchange."
    )
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    # The engine tool is the only tool on offer, under the report budget --
    # not explain()'s budget, even though the two happen to share a value.
    assert options.max_turns == 8
    assert options.tools == []
    assert options.allowed_tools == ["mcp__engine__analyze_position"]
    mcp_servers = options.mcp_servers
    assert isinstance(mcp_servers, dict)
    assert "engine" in mcp_servers


async def test_agent_sdk_provider_complete_without_analyst_stays_single_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no analyst, complete() is one turn, no MCP server, and the
    same built-in-tool lockdown as every other provider path — a
    coaching run must never reach Claude Code's file or shell tools."""
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Work on your endgames.")],
                model="claude-opus-4-8",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    assert options.max_turns == 1
    assert options.tools == []  # built-ins locked down, like explain()
    assert options.allowed_tools == []
    assert not options.mcp_servers


def test_provider_satisfies_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig())
    assert isinstance(provider, ClaudeAgentSdkProvider)


# --- build_move_context ---------------------------------------------------

_EXPLAIN_EVALS = [
    MoveEval(
        ply=1, san="e4", eval_cp=30, eval_mate=None,
        best_move="e2e4", cp_loss=0, judgment="best",
    ),
    MoveEval(
        ply=2, san="e5", eval_cp=30, eval_mate=None,
        best_move="e7e5", cp_loss=0, judgment="best",
    ),
    MoveEval(
        ply=3, san="Nf3", eval_cp=-270, eval_mate=None,
        best_move="d2d4", cp_loss=300, judgment="blunder",
    ),
]  # fmt: skip


def test_build_move_context_normal_ply() -> None:
    analysis = make_analysis(game_id="g-ctx").model_copy(
        update={"evals": _EXPLAIN_EVALS}
    )
    game = make_game(id="g-ctx", san_moves=["e4", "e5", "Nf3"], color="white")

    ctx = build_move_context(game, analysis, RUY, ply=3)

    assert ctx.username == "testuser"
    assert ctx.color == "white"
    assert ctx.opening_name == "Ruy Lopez"
    assert ctx.ply == 3
    assert ctx.san == "Nf3"
    assert ctx.fen_before.startswith(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w"
    )
    assert ctx.fen_after.startswith(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b"
    )
    assert ctx.best_move == "d4"  # UCI d2d4 rendered as SAN on fen_before
    assert ctx.cp_loss == 300
    assert ctx.judgment == "blunder"


def test_build_move_context_first_ply_uses_starting_position() -> None:
    evals = [_EXPLAIN_EVALS[0]]
    analysis = make_analysis(game_id="g-ctx").model_copy(update={"evals": evals})
    game = make_game(id="g-ctx", san_moves=["e4"], color="white")

    ctx = build_move_context(game, analysis, opening=None, ply=1)

    assert ctx.fen_before == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert ctx.san == "e4"
    assert ctx.opening_name is None


def test_build_move_context_out_of_range_ply_raises() -> None:
    analysis = make_analysis(game_id="g-ctx").model_copy(
        update={"evals": _EXPLAIN_EVALS}
    )
    game = make_game(id="g-ctx", san_moves=["e4", "e5", "Nf3"])

    with pytest.raises(ValueError, match="out of range"):
        build_move_context(game, analysis, opening=None, ply=4)
    with pytest.raises(ValueError, match="out of range"):
        build_move_context(game, analysis, opening=None, ply=0)


# --- render_explain_prompt -------------------------------------------------

_EXPLAIN_CTX = MoveContext(
    username="testuser",
    color="white",
    opening_name="Ruy Lopez",
    ply=3,
    san="Nf3",
    fen_before="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1",
    fen_after="rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 1",
    best_move="d4",
    cp_loss=300,
    judgment="blunder",
)


def test_render_explain_prompt_is_deterministic_and_complete() -> None:
    lines = [
        EvalLine(
            multipv=1,
            depth=18,
            eval_cp=35,
            eval_mate=None,
            pv_san=["d4", "exd4", "Nxd4", "Nf6", "Nc3", "Bb4"],
        ),
        EvalLine(
            multipv=2, depth=18, eval_cp=10, eval_mate=None, pv_san=["Bc4", "Nf6"]
        ),
    ]

    prompt = render_explain_prompt(_EXPLAIN_CTX, lines)

    assert prompt == render_explain_prompt(_EXPLAIN_CTX, lines)
    assert "## Move explanation for testuser" in prompt
    assert "testuser was playing white in a Ruy Lopez game." in prompt
    assert f"`{_EXPLAIN_CTX.fen_before}`" in prompt
    assert f"`{_EXPLAIN_CTX.fen_after}`" in prompt
    assert "played **Nf3** (lost about 3.0 pawns; judged **blunder**)" in prompt
    assert "engine's preferred **d4**" in prompt
    assert "| 1 | 18 | +0.35 | d4 exd4 Nxd4 Nf6 Nc3 … |" in prompt  # truncated pv
    assert "| 2 | 18 | +0.10 | Bc4 Nf6 |" in prompt
    assert "analyze_position" in prompt
    assert "300" not in prompt  # raw centipawns never reach the model
    assert "club player" in prompt
    assert "never centipawns" in prompt


def test_render_explain_prompt_mate_scale_no_opening_no_lines() -> None:
    ctx = _EXPLAIN_CTX.model_copy(
        update={"opening_name": None, "color": "black", "cp_loss": 10_050}
    )

    prompt = render_explain_prompt(ctx, [])

    assert "testuser was playing black." in prompt
    assert "walked into a forced mate; judged **blunder**" in prompt
    assert "10050" not in prompt
    assert "Candidate lines" not in prompt


# --- ClaudeAgentSdkProvider.explain -----------------------------------------


async def stub_analyst(fen: str) -> list[EvalLine]:
    return [
        EvalLine(multipv=1, depth=12, eval_cp=-40, eval_mate=None,
                  pv_san=["Bxf7+", "Kxf7"]),
    ]  # fmt: skip


async def test_agent_sdk_provider_explain_streams_text_and_tool_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Let's look at this position.")],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1",
                        name="mcp__engine__analyze_position",
                        input={"fen": "fen-after"},
                    )
                ],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[TextBlock(text="Black wins the exchange next.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=2,
                session_id="s",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    assert events == [
        ExplainEvent(type="text", text="Let's look at this position."),
        ExplainEvent(type="tool", text="engine: analyzing fen-after"),
        ExplainEvent(type="text", text="Black wins the exchange next."),
    ]
    assert captured["prompt"] == "explain this move"
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    assert options.max_turns == 8
    assert options.tools == []
    assert options.allowed_tools == ["mcp__engine__analyze_position"]
    mcp_servers = options.mcp_servers
    assert isinstance(mcp_servers, dict)
    assert "engine" in mcp_servers


async def test_agent_sdk_provider_explain_surfaces_error_detail_from_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=0,
                session_id="s",
                result="Not logged in · Please run /login",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="Not logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        raise FileNotFoundError("claude binary not found")

    monkeypatch.setattr(providers_module, "query", broken_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                result=None,
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_early_close_closes_the_sdk_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A client disconnect closes explain() mid-stream; that close must
    # reach the SDK query generator now, not wait for GC finalization.
    sdk_stream_closed = False

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            nonlocal sdk_stream_closed
            try:
                yield AssistantMessage(
                    content=[TextBlock(text="First thought.")],
                    model="claude-opus-4-8",
                )
                yield AssistantMessage(
                    content=[TextBlock(text="Never consumed.")],
                    model="claude-opus-4-8",
                )
            finally:
                sdk_stream_closed = True

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    events = provider.explain("explain this move", stub_analyst)
    first = await anext(events)
    assert first == ExplainEvent(type="text", text="First thought.")
    await events.aclose()
    assert sdk_stream_closed


# --- CopilotSdkProvider -----------------------------------------------------
#
# copilot.CopilotClient/CopilotSession are callback-based (session.on(...)
# dispatches SessionEvents synchronously), unlike the Claude Agent SDK's
# async-generator query(). These fakes replay a scripted step list against
# the same on()/send() surface: "text" dispatches an AssistantMessageData
# event, "tool_call" invokes the registered analyze_position tool handler
# directly (standing in for the CLI runtime deciding to call it), "error"
# dispatches a SessionErrorData event, and "idle" dispatches SessionIdleData.


class _FakeCopilotSession:
    def __init__(
        self,
        script: list[tuple[str, str]],
        tools: list[Tool] | None,
        captured: dict[str, object],
    ) -> None:
        self._script = script
        self._tools = {t.name: t for t in (tools or [])}
        self._handlers: list[Callable[[SessionEvent], None]] = []
        self._captured = captured

    def on(self, handler: Callable[[SessionEvent], None]) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def send(self, prompt: str, **_: object) -> str:
        self._captured["prompt"] = prompt
        tool_results: list[ToolResult] = []
        for kind, value in self._script:
            if kind == "text":
                self._dispatch(
                    AssistantMessageData(content=value, message_id="m"),
                    SessionEventType.ASSISTANT_MESSAGE,
                )
            elif kind == "tool_call":
                tool_obj = self._tools["analyze_position"]
                assert tool_obj.handler is not None
                # ToolHandler's declared return type is ToolResult |
                # Awaitable[ToolResult]; the provider always registers an
                # async handler, so this narrowing is safe.
                handler = cast(
                    "Callable[[ToolInvocation], Awaitable[ToolResult]]",
                    tool_obj.handler,
                )
                result = await handler(
                    ToolInvocation(
                        tool_name="analyze_position", arguments={"fen": value}
                    )
                )
                tool_results.append(result)
            elif kind == "error":
                self._dispatch(
                    SessionErrorData(error_type="model_error", message=value),
                    SessionEventType.SESSION_ERROR,
                )
            elif kind == "idle":
                self._dispatch(SessionIdleData(), SessionEventType.SESSION_IDLE)
        self._captured["tool_results"] = tool_results
        return "message-id"

    def _dispatch(self, data: object, event_type: SessionEventType) -> None:
        event = SessionEvent(
            data=data,  # type: ignore[arg-type]  -- fake narrows by construction
            id=uuid4(),
            timestamp=datetime.now(UTC),
            type=event_type,
        )
        for handler in list(self._handlers):
            handler(event)

    async def __aenter__(self) -> "_FakeCopilotSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._captured["session_disconnected"] = True


class _FakeCopilotClient:
    def __init__(
        self,
        script: list[tuple[str, str]],
        captured: dict[str, object],
        *,
        fail_on_enter: Exception | None = None,
    ) -> None:
        self._script = script
        self._captured = captured
        self._fail_on_enter = fail_on_enter

    async def __aenter__(self) -> "_FakeCopilotClient":
        if self._fail_on_enter is not None:
            raise self._fail_on_enter
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._captured["client_stopped"] = True

    async def create_session(
        self,
        *,
        model: str | None = None,
        system_message: object | None = None,
        available_tools: object | None = None,
        tools: list[Tool] | None = None,
        **_: object,
    ) -> _FakeCopilotSession:
        self._captured["model"] = model
        self._captured["system_message"] = system_message
        self._captured["available_tools"] = available_tools
        return _FakeCopilotSession(self._script, tools, self._captured)


def _fake_copilot_client(
    script: list[tuple[str, str]],
    captured: dict[str, object],
    *,
    fail_on_enter: Exception | None = None,
) -> Callable[[], _FakeCopilotClient]:
    def factory(*_: object, **__: object) -> _FakeCopilotClient:
        return _FakeCopilotClient(script, captured, fail_on_enter=fail_on_enter)

    return factory


async def test_copilot_provider_complete_collects_text_across_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("text", "Work on your "), ("text", "endgames."), ("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    assert captured["prompt"] == "coach me"
    system_message = captured["system_message"]
    assert isinstance(system_message, dict)
    assert system_message["mode"] == "replace"
    assert "chess coach" in system_message["content"]
    # A one-shot completion gets no tools at all.
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == []


async def test_copilot_provider_complete_surfaces_error_detail_from_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("error", "Not logged in · Please run /login")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="Not logged in"):
        await provider.complete("coach me")


async def test_copilot_provider_complete_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_copilot_client(
            [], captured, fail_on_enter=FileNotFoundError("copilot runtime missing")
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        await provider.complete("coach me")


async def test_copilot_provider_complete_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="returned no text"):
        await provider.complete("coach me")


async def test_copilot_provider_complete_with_analyst_concatenates_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/fixes-2026-07/04-report-engine-tool.md: given an analyst,
    complete() registers analyze_position exactly as explain() does, and
    text either side of an engine call concatenates into the returned
    advice."""
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's verify the critical line. "),
        ("tool_call", "fen-after"),
        ("text", "Confirmed: Nxe5 wins the exchange."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await provider.complete("write the report", stub_analyst)

    assert advice == (
        "Let's verify the critical line. Confirmed: Nxe5 wins the exchange."
    )
    # The engine tool is the only tool on offer -- same lockdown as explain().
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == ["custom:analyze_position"]


async def test_copilot_provider_complete_without_analyst_gets_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no analyst, complete() must degrade to exactly today's
    behavior: the session gets an empty ToolSet, no custom tool at all."""
    captured: dict[str, object] = {}
    script = [("text", "Work on your endgames."), ("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == []


async def test_copilot_provider_complete_enforces_report_turn_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirrors test_copilot_provider_explain_caps_engine_tool_calls: the SDK
    # has no turn limit, so the provider counts engine-tool calls itself
    # against _REPORT_MAX_TURNS. Every call past the budget -- the one
    # grace round and the runaway calls after it -- gets steered to wrap up
    # instead of reaching the engine.
    max_engine_calls = 8  # _REPORT_MAX_TURNS
    captured: dict[str, object] = {}
    over_budget = 3
    script = [("tool_call", f"fen-{n}") for n in range(max_engine_calls + over_budget)]
    script.append(("text", "Here's what I found."))
    script.append(("idle", ""))
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await provider.complete("write the report", stub_analyst)

    assert advice == "Here's what I found."
    # _FakeCopilotSession.send stores a list[ToolResult]; captured erases
    # that to plain object for its other (str, bool) entries.
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == max_engine_calls + over_budget
    for result in tool_results[max_engine_calls:]:
        assert isinstance(result, ToolResult)
        assert result.result_type == "success"
        assert "budget" in result.text_result_for_llm


async def test_copilot_provider_complete_runaway_tool_calls_cut_the_run_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirrors test_copilot_provider_explain_runaway_tool_calls_end_the_stream,
    # adapted to complete()'s collection shape: instead of explain()'s
    # queue-based drain sentinel, the runaway branch sets the idle event
    # directly, so `await idle.wait()` returns and the `async with` blocks
    # disconnect the session -- text collected before the runaway stands.
    # No "idle" step is scripted at all: if the implementation failed to
    # set the event on the runaway call, this would hang instead of pass,
    # so the timeout wrapper turns that failure mode into a clean failure.
    max_engine_calls = 8  # _REPORT_MAX_TURNS
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's check a few lines."),
        *[("tool_call", f"fen-{n}") for n in range(max_engine_calls)],  # in budget
        ("tool_call", "fen-grace"),  # budget + 1: the one grace round
        ("tool_call", "fen-runaway"),  # past the grace round: hard stop
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await asyncio.wait_for(
        provider.complete("write the report", stub_analyst), timeout=5
    )

    assert advice == "Let's check a few lines."
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == max_engine_calls + 2
    assert "budget" in tool_results[-1].text_result_for_llm
    # Teardown still ran: the `async with` blocks disconnected on the idle
    # event set from inside the runaway call, not from a scripted idle.
    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


async def test_copilot_provider_explain_streams_tool_and_text_events_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's look at this position."),
        ("tool_call", "fen-after"),
        ("text", "Black wins the exchange next."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    assert events == [
        ExplainEvent(type="text", text="Let's look at this position."),
        ExplainEvent(type="tool", text="engine: analyzing fen-after"),
        ExplainEvent(type="text", text="Black wins the exchange next."),
    ]
    assert captured["prompt"] == "explain this move"
    # Only the engine tool is admitted — no shell/file/web built-ins.
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == ["custom:analyze_position"]


async def test_copilot_provider_explain_surfaces_error_detail_from_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("error", "Not logged in · Please run /login")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="Not logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_copilot_client(
            [], captured, fail_on_enter=FileNotFoundError("copilot runtime missing")
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_caps_engine_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike ClaudeAgentSdkProvider's SDK-enforced max_turns == 8 hard stop
    # (see test_agent_sdk_provider_explain_streams_text_and_tool_events),
    # the Copilot SDK has no built-in turn limit, so the provider counts
    # engine-tool calls itself: every call past the budget — the one grace
    # round and any runaway call after it — gets steered to wrap up instead
    # of reaching the engine. See
    # test_copilot_provider_explain_runaway_tool_calls_end_the_stream for
    # the hard-stop behavior once the grace round is also exhausted.
    max_engine_calls = 8
    captured: dict[str, object] = {}
    over_budget = 3
    script = [("tool_call", f"fen-{n}") for n in range(max_engine_calls + over_budget)]
    script.append(("text", "Here's what I found."))
    script.append(("idle", ""))
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_engine_calls

    # _FakeCopilotSession.send stores a list[ToolResult]; captured erases
    # that to plain object for its other (str, bool) entries.
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == max_engine_calls + over_budget
    # Calls past the budget don't reach the engine — they get steered to
    # wrap up instead.
    for result in tool_results[max_engine_calls:]:
        assert isinstance(result, ToolResult)
        assert result.result_type == "success"
        assert "budget" in result.text_result_for_llm


async def test_copilot_provider_explain_runaway_tool_calls_end_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After the one grace round past the budget, a further engine-tool call
    # is a runaway: the provider must cut the stream off — the generator
    # ends and the session gets disconnected — rather than let a looping
    # model keep calling the engine forever.
    max_engine_calls = 8
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's check a few lines."),
        *[("tool_call", f"fen-{n}") for n in range(max_engine_calls)],  # in budget
        ("tool_call", "fen-grace"),  # budget + 1: the one grace round
        ("tool_call", "fen-runaway"),  # past the grace round: hard stop
        ("text", "Should never be yielded."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    # The stream ends right after the in-budget tool events: the grace
    # round produces no event, and the runaway call's drain sentinel cuts
    # the loop off before the trailing text/idle steps are ever reached.
    assert events[0] == ExplainEvent(type="text", text="Let's check a few lines.")
    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_engine_calls
    assert events[-1] == tool_events[-1]

    # Teardown still ran even though nothing called aclose() explicitly —
    # the generator's own `async with` blocks disconnected on completion.
    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


async def test_copilot_provider_explain_early_close_disconnects_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [
        ("text", "First thought."),
        ("text", "Never consumed."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = provider.explain("explain this move", stub_analyst)
    first = await anext(events)
    assert first == ExplainEvent(type="text", text="First thought.")
    await events.aclose()

    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


def test_copilot_provider_satisfies_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig(provider="github-copilot"))
    assert isinstance(provider, CopilotSdkProvider)


async def test_copilot_provider_complete_times_out_when_session_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that never goes idle or errors (a wedged CLI runtime)
    must fail the request instead of hanging it forever (scan finding
    7, docs/CODEBASE-SCAN-2026-07.md)."""
    captured: dict[str, object] = {}
    script = [("text", "half an answer, then silence")]  # no idle, no error
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )
    monkeypatch.setattr(providers_module, "_SESSION_STALL_TIMEOUT", 0.05)

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="stalled"):
        await asyncio.wait_for(provider.complete("coach me"), timeout=1)
    # The timeout path still tears the session down.
    assert captured["session_disconnected"] is True


async def test_copilot_provider_explain_times_out_when_session_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same stall guard for the drain loop: events already streamed
    stand, then silence surfaces as an error instead of a hang."""
    captured: dict[str, object] = {}
    script = [("text", "The bishop was ")]  # never idles
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )
    monkeypatch.setattr(providers_module, "_SESSION_STALL_TIMEOUT", 0.05)

    provider = create_provider(LlmConfig(provider="github-copilot"))
    streamed: list[str] = []
    with pytest.raises(CoachProviderError, match="stalled"):

        async def drain() -> None:
            async for event in provider.explain("explain", stub_analyst):
                streamed.append(event.text)

        await asyncio.wait_for(drain(), timeout=1)
    assert streamed == ["The bishop was "]
    assert captured["session_disconnected"] is True
