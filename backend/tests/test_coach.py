"""Coach component tests (docs/06-coach.md)."""

import asyncio
import re
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
    PROFILE_PROMPT_VERSION,
    PROMPT_VERSION,
    ClaudeAgentSdkProvider,
    CoachProvider,
    CoachProviderError,
    CopilotSdkProvider,
    ExplainEvent,
    MoveContext,
    append_game_links,
    build_move_context,
    build_profile,
    build_report,
    create_provider,
    render_explain_prompt,
    render_profile_context,
    render_profile_prompt,
    render_prompt,
    render_report_chat_context,
)
from chess_coach.coach.prompt import SYSTEM_PROMPT
from chess_coach.domain import (
    AnalyzedGame,
    Color,
    ErrorPattern,
    EvalLine,
    GameSummary,
    LlmConfig,
    MoveEval,
    Opening,
    Phase,
    PlayerReport,
    Record,
    Result,
    TimeClass,
)
from tests.coach_scenario import scenario_games
from tests.factories import make_analysis, make_analyzed, make_game, summarize
from tests.snapshots import write_or_check

RUY = Opening(eco="C60", name="Ruy Lopez", ply=5)
PROMPT_SNAPSHOT = Path(__file__).parent / "testdata" / "coach_prompt.md"
PROFILE_PROMPT_SNAPSHOT = Path(__file__).parent / "testdata" / "coach_profile_prompt.md"
PROFILE_CONTEXT_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_profile_context.md"
)
PROFILE_CONTEXT_NO_NARRATIVE_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_profile_context_no_narrative.md"
)
EXPLAIN_PROMPT_WITH_PROFILE_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_explain_prompt_with_profile.md"
)


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
    assert critical.opponent == "hikaru"  # make_game's default opponent
    assert critical.time_class == "blitz"
    assert critical.end_time == 1_781_000_000
    assert critical.opening_name == "Ruy Lopez"
    assert critical.ply == 3
    assert critical.move_number == 2
    assert critical.leading_up == ["e4", "e5"]
    assert critical.eval_before_cp == 30
    assert critical.eval_after_cp == -270


def test_englund_attributed_to_opponent_not_student_repertoire() -> None:
    """The Englund regression (docs/archive/coach-report-improvements.md finding 1) and
    its faced/chosen split (docs/archive/fixes-2026-07/03-faced-openings.md).

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
    prompt must render the row under "What they face as White" -- never
    under "Systems the student chose", where a misattributed line would invite
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
    chosen_part, faced_part = white_section.split("#### What they face as White")
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
    assert patterns["hangs_piece"].example_opponent == "hikaru"  # make_game default


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
    write_or_check(PROMPT_SNAPSHOT, prompt)


def test_no_template_says_acpl_anywhere() -> None:
    """docs/06-coach.md, "Units": one scale, one name. Every loss figure
    the model or the student sees is pawns, and nothing calls a pawns
    figure "ACPL" -- the acronym expands to average *centipawn* loss, so
    a template using it either contradicts its own number or teaches the
    model a word for the wrong scale. The glossary line that used to
    reconcile the two is gone; this is what replaces it.
    """
    games = scenario_games()
    report = build_report(
        "testuser",
        games,
        all_games=[summarize(g, opening=g.opening) for g in games],
        games_in_scope=len(games) + 5,  # renders the partial-coverage caveat too
    )
    profile = build_profile(report)
    lines = [EvalLine(multipv=1, depth=18, eval_cp=35, eval_mate=None, pv_san=["d4"])]

    # Each document paired with the marker that opens its instruction
    # block. The block is prose *about* how to write, where naming a
    # unit to forbid it ("pawns, never centipawns") is the whole point;
    # everything before it is data, labels and headers, where a figure
    # and its unit sit together and the acronym must never appear.
    rendered = {
        "brief": (render_prompt(report), "## Instructions"),
        "profile prompt": (render_profile_prompt(profile), "## Instructions"),
        "profile context": (render_profile_context(profile), None),
        "explain": (
            render_explain_prompt(_EXPLAIN_CTX, lines, profile=profile),
            "Explain why the played move loses",
        ),
        "report chat seed": (
            render_report_chat_context(report, engine_available=True),
            "## How to respond",
        ),
        "system prompt": (SYSTEM_PROMPT, None),
    }

    for name, (text, marker) in rendered.items():
        data = text if marker is None else text.split(marker)[0]
        assert marker is None or marker in text, f"{name} lost its instructions"
        assert "ACPL" not in data, f"{name} labels a figure ACPL"
        assert "centipawn" not in data.lower(), f"{name} names centipawns in its data"


def test_render_prompt_data_describes_the_student_in_one_register() -> None:
    """docs/06-coach.md, "One register per document": the data half
    describes the student in the third person, as the instruction block
    already did. Second person in this template is the model's, in the
    instructions, and the student's, in the brief the model writes --
    never the data's, which would leave one document in two registers.
    """
    report = build_report("testuser", scenario_games())
    data, instructions = render_prompt(report).split("## Instructions")

    assert not re.findall(r"\byou(?:r|rs)?\b", data, flags=re.IGNORECASE)
    assert "#### Systems the student chose" in data
    assert "Played **6.Bd3**" in data
    # ...while the output register the instructions demand is unchanged.
    assert "in the second person" in instructions


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
    assert "Note: the other 6 games in scope are not engine-analyzed." in prompt


def test_partial_coverage_caveat_names_which_figures_it_touches() -> None:
    """The caveat used to say "every figure below describes only the
    analyzed span", which the volume/quality split made false: ratings,
    records and milestones cover every game (docs/06-coach.md, "Volume
    and quality"). Telling the model otherwise discounts the half of
    the brief that is complete."""
    prompt = render_prompt(
        build_report("testuser", scenario_games(), games_in_scope=25)
    )

    assert "figure below describes only the analyzed span" not in prompt
    assert "Ratings, records, milestones" in prompt
    assert "cover every game in scope" in prompt


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


def test_instructions_contain_opponent_first_citation_rule() -> None:
    """docs/06-coach.md's rewritten Citation bullet: game first (opponent
    and date), move second (the reference link through the handle)."""
    prompt = render_prompt(build_report("testuser", scenario_games()))
    assert "Game first, move second" in prompt
    assert 'e.g. "In your game against marko77 on June 14, [26...Nb6][g3]' in prompt
    assert "never an invented handle" in prompt
    assert 'may shorten (e.g. "that marko77 game")' in prompt
    assert "opening name appears only as coaching content" in prompt


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
        example_opponent=turning_point.opponent,
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


def test_turning_point_heading_includes_opponent_for_every_entry() -> None:
    """docs/06-coach.md: every turning point's identity now includes the
    opponent, so the model has it in hand to name the game by opponent
    and date (the new Citation rule) instead of just a date."""
    report = build_report("testuser", scenario_games())
    prompt = render_prompt(report)

    assert report.critical_positions
    for position in report.critical_positions:
        color_word = "White" if position.color == "white" else "Black"
        assert f"as {color_word} vs {position.opponent}" in prompt


def test_turning_point_heading_places_opponent_between_color_and_opening() -> None:
    """Rendered heading layout (locked by the snapshot, not prescribed
    by the doc): "... as White vs marko77, <opening> -- move N -- cite
    [gN]" -- opponent sits right after the color word and before the
    opening name."""
    report = build_report("testuser", scenario_games())
    first = report.critical_positions[0].model_copy(update={"opponent": "marko77"})
    report = report.model_copy(update={"critical_positions": [first]})
    prompt = render_prompt(report)

    color_word = "White" if first.color == "white" else "Black"
    opening = f", {first.opening_name}" if first.opening_name else ""
    assert (
        f"as {color_word} vs marko77{opening} -- move {first.move_number} "
        "-- cite [g1]" in prompt
    )


def test_error_example_cell_includes_opponent() -> None:
    """Rendered Example-cell layout (locked by the snapshot, not
    prescribed by the doc): "<date> vs <opponent>, <side>'s move N
    (cite [gN])" -- opponent sits right after the date, before the
    move-number clause."""
    report = build_report("testuser", scenario_games())
    pattern = report.error_patterns[0].model_copy(
        update={"example_opponent": "dimitris88"}
    )
    report = report.model_copy(update={"error_patterns": [pattern]})
    prompt = render_prompt(report)

    error_section = prompt.split("## Recurring error patterns")[1].split(
        "## Turning points"
    )[0]
    assert " vs dimitris88, " in error_section
    assert "'s move" in error_section  # the move-number clause still follows


def test_error_example_omits_vs_clause_when_opponent_missing() -> None:
    """Stale data (an example predating `example_opponent`) must render
    without a "vs ..." clause rather than "vs None" -- the pre-opponent
    citation format, not a crash or a literal "None"."""
    report = build_report("testuser", scenario_games())
    pattern = report.error_patterns[0].model_copy(update={"example_opponent": None})
    report = report.model_copy(update={"error_patterns": [pattern]})
    prompt = render_prompt(report)

    error_section = prompt.split("## Recurring error patterns")[1].split(
        "## Turning points"
    )[0]
    assert "vs None" not in error_section
    assert " vs " not in error_section


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


# --- player profile (docs/06-coach.md, "Player profile") ------------------


def test_profile_prompt_version_is_independent_of_prompt_version() -> None:
    assert PROFILE_PROMPT_VERSION == "profile-v4"
    assert PROFILE_PROMPT_VERSION != PROMPT_VERSION


def test_profile_instructions_constrain_what_the_embed_cannot_fix() -> None:
    """docs/06-coach.md, "Narrative": the narrative is written under one
    prompt and read under others, so two of its rules are about the trip
    rather than the content. `render_profile_context` block-quotes the
    text, which bounds a stray heading; nothing downstream can repair a
    unit the narrative never spelled.
    """
    prompt = render_profile_prompt(build_profile(build_report("testuser", [])))
    instructions = prompt.split("## Instructions")[1]

    assert '"1.30 ACPL"' in instructions  # named as the thing not to write
    assert "markdown headings" in instructions


def test_build_profile_copies_report_scalars_and_denominators() -> None:
    """games_covered/window/player_moves/overall_acpl/judgment_counts/
    phases/time_classes carry no aggregation of their own -- build_profile
    copies them straight off the report (docs/06-coach.md)."""
    report = build_report("testuser", scenario_games())
    profile = build_profile(report)

    assert profile.username == report.username
    assert profile.games_covered == report.games_analyzed
    assert profile.window_start == report.window_start
    assert profile.window_end == report.window_end
    assert profile.player_moves == report.player_moves
    assert profile.overall_acpl == report.overall_acpl
    assert profile.judgment_counts == report.judgment_counts
    assert profile.phases == report.phases
    assert profile.time_classes == report.time_classes
    assert profile.narrative is None


def test_build_profile_months_capped_to_last_six_oldest_first() -> None:
    """docs/06-coach.md keeps only "the most recent months of trend" as
    contract and leaves the cap to the tests; build_profile keeps the
    last six, oldest first. Eight distinct months of games so the cap
    itself -- not just the fixture's natural span -- is under test."""
    games = [
        make_analyzed(
            f"g-{month}",
            ["e4", "e5"],
            color="white",
            result="win",
            end_time=int(datetime(2026, month, 15, tzinfo=UTC).timestamp()),
        )
        for month in range(1, 9)  # January through August: 8 distinct months
    ]
    profile = build_profile(build_report("testuser", games))

    assert [m.month for m in profile.months] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]  # the 6 most recent, oldest first -- January and February dropped


def test_build_profile_error_patterns_reuse_the_report_list() -> None:
    report = build_report("testuser", scenario_games())
    profile = build_profile(report)

    assert profile.error_patterns == report.error_patterns


def test_build_profile_repertoire_rows_agree_with_report_prompt_tables() -> None:
    """Cross-checks docs/06-coach.md's requirement that the profile's
    repertoire rows reuse the *exact* family rollup the report prompt
    renders -- not a second implementation that merely happens to agree
    today. Every number here is read off testdata/coach_prompt.md's
    "Systems the student chose" / "What they face" tables for this same
    scenario.
    """
    report = build_report("testuser", scenario_games())
    profile = build_profile(report)
    by_key = {(o.color, o.faced, o.name): o for o in profile.openings}

    chosen_white = by_key[("white", False, "Queen's Pawn Game")]
    assert chosen_white.moves == "1.d4 2.Bf4 3.e3"  # the family's shared `system`
    assert chosen_white.games == 6
    assert chosen_white.score == pytest.approx(0.583, abs=0.001)  # 58% in the table

    faced_white = by_key[("white", True, "Englund Gambit Complex")]
    # faced rows carry the full line (both sides), never a `system` --
    # the Englund is the *opponent's* choice (docs/06-coach.md).
    assert faced_white.moves == "1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7"
    assert faced_white.games == 5
    assert faced_white.score == pytest.approx(0.4, abs=0.001)  # 40% in the table

    chosen_black = by_key[("black", False, "Pirc Defense")]
    # Two lichess names (Classical, Austrian Attack) roll up into one
    # family, same as the report prompt's "Pirc Defense" row.
    assert chosen_black.moves == "1...d6 2...Nf6 3...g6"
    assert chosen_black.games == 6


def test_build_profile_excludes_below_floor_families() -> None:
    """The two-game Ruy Lopez family never clears the 5-game sample floor
    (docs/06-coach.md, "Sample floor and sort"), and Black has no faced
    row above it either (the report prompt renders "No line yet reaches
    the 5-game sample floor" there) -- both must be absent from the
    profile. Unlike the report prompt, the profile has no long-tail line
    to fall back into: a below-floor family simply never appears.
    """
    report = build_report("testuser", scenario_games())
    profile = build_profile(report)

    names = {(o.color, o.faced, o.name) for o in profile.openings}
    assert ("white", False, "Ruy Lopez") not in names
    assert not any(color == "black" and faced for color, faced, _ in names)
    assert len(profile.openings) == 3  # white chosen, white faced, black chosen


_CHOSEN_LINES: dict[str, list[str]] = {
    "System Alpha": ["d4", "d5", "c4", "e6", "Nc3", "Nf6"],
    "System Beta": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
    "System Gamma": ["Nf3", "d5", "g3", "Nf6", "Bg2", "e6"],
    "System Delta": ["c4", "e5", "Nc3", "Nf6", "g3", "d5"],
}


def _white_chosen_games(
    prefix: str, name: str, eco: str, count: int
) -> list[AnalyzedGame]:
    """`count` White wins in a system distinct from every other system
    below -- ply 5 is White's own 3rd move, an odd ply, which resolves
    `faced=False` for a White row (docs/06-coach.md)."""
    return [
        make_analyzed(
            f"{prefix}-{i}",
            _CHOSEN_LINES[name],
            color="white",
            result="win",
            opening=Opening(eco=eco, name=name, ply=5),
        )
        for i in range(count)
    ]


def _white_faced_games(
    prefix: str, name: str, eco: str, results: list[Result]
) -> list[AnalyzedGame]:
    """One game per entry in `results` -- ply 2 is Black's reply, an even
    ply, which resolves `faced=True` for a White row (docs/06-coach.md).
    """
    return [
        make_analyzed(
            f"{prefix}-{i}",
            ["d4", "e5", "dxe5"],
            color="white",
            result=result,
            opening=Opening(eco=eco, name=name, ply=2),
        )
        for i, result in enumerate(results)
    ]


def test_build_profile_caps_chosen_and_faced_rows_per_color() -> None:
    """The caps are implementation detail by contract (docs/06-coach.md
    leaves them to the tests): build_profile keeps the top 3 chosen
    families per color by games played and the top 2 faced families per
    color by impact (games x win-rate deficit -- the report tables' own
    sort, docs/06-coach.md "Sample floor and sort"). Four chosen and
    three faced families all clear the 5-game sample floor here, so the
    cap -- not the floor -- is what must bind.
    """
    games = [
        *_white_chosen_games("a", "System Alpha", "A01", 9),
        *_white_chosen_games("b", "System Beta", "A02", 8),
        *_white_chosen_games("c", "System Gamma", "A03", 7),
        *_white_chosen_games("d", "System Delta", "A04", 6),
        *_white_faced_games("e", "Gambit Echo", "B01", ["loss"] * 6),
        *_white_faced_games(
            "f", "Gambit Foxtrot", "B02", ["loss", "loss", "loss", "win", "win"]
        ),
        *_white_faced_games(
            "g", "Gambit Golf", "B03", ["win", "win", "win", "win", "loss"]
        ),
    ]
    profile = build_profile(build_report("testuser", games))

    chosen_names = [o.name for o in profile.openings if not o.faced]
    faced_names = [o.name for o in profile.openings if o.faced]
    # Impact: Echo 6*(0.5-0)=3.0, Foxtrot 5*(0.5-0.4)=0.5, Golf
    # 5*(0.5-0.8)=-1.5 -- Golf is winning, so it has the *least* impact
    # and is the one the cap must drop.
    assert chosen_names == ["System Alpha", "System Beta", "System Gamma"]
    assert faced_names == ["Gambit Echo", "Gambit Foxtrot"]


def test_build_profile_empty_report_is_total() -> None:
    """No analyzed games -> every list empty, every count zero, no crash:
    build_profile must stay total over an empty report."""
    profile = build_profile(build_report("testuser", []))

    assert profile.games_covered == 0
    assert profile.window_start is None
    assert profile.window_end is None
    assert profile.player_moves == 0
    assert profile.overall_acpl == 0.0
    assert profile.judgment_counts == dict.fromkeys(
        ("best", "good", "inaccuracy", "mistake", "blunder"), 0
    )
    assert profile.time_classes == []
    assert profile.months == []
    assert profile.openings == []
    assert profile.error_patterns == []
    assert profile.narrative is None
    assert all(
        stats.moves == 0 and stats.acpl is None for stats in profile.phases.values()
    )


def test_render_profile_prompt_matches_snapshot() -> None:
    """docs/06-coach.md: render_profile_prompt is snapshot-tested like
    every other coach prompt. Regenerate deliberately with
    `UPDATE_SNAPSHOTS=1 uv run pytest -k snapshot`, then read the diff of
    testdata/coach_profile_prompt.md as the review artifact.
    """
    profile = build_profile(build_report("testuser", scenario_games()))
    prompt = render_profile_prompt(profile)

    assert prompt == render_profile_prompt(profile), (
        "render_profile_prompt is not deterministic"
    )
    write_or_check(PROFILE_PROMPT_SNAPSHOT, prompt)


def test_render_profile_prompt_forbids_game_citations_in_instructions() -> None:
    profile = build_profile(build_report("testuser", scenario_games()))
    prompt = render_profile_prompt(profile)

    assert "No game citations" in prompt
    assert "cite [g" not in prompt  # no link-handle apparatus offered at all


def test_render_profile_prompt_briefs_the_coach_in_the_third_person() -> None:
    """The narrative is stored and pasted into other prompts, where the
    reader is another coach -- so v1's 'address the student directly as
    "you"' told that coach *they* were the one hanging pieces
    (docs/06-coach.md, "Narrative"). The instructions must now forbid
    the second person rather than require it."""
    profile = build_profile(build_report("testuser", scenario_games()))
    prompt = render_profile_prompt(profile)

    assert 'never address the reader as "you"' in prompt
    assert "third person" in prompt
    assert "address the student directly" not in prompt


def test_render_profile_prompt_facts_never_address_the_student() -> None:
    """Not just the instructions: the rendered facts themselves said
    "Systems you chose", which models copy into the narrative."""
    profile = build_profile(build_report("testuser", scenario_games()))
    facts = render_profile_prompt(profile).split("## Instructions")[0]

    assert "Systems the student chose:" in facts
    assert "Systems you chose:" not in facts
    assert "What you face" not in facts


def test_render_profile_prompt_empty_profile_has_no_empty_sections() -> None:
    """Section *headers*, not bare words: the instruction block names
    "Ratings" and "Recent form" in its own prose, so a substring check
    for those would pass only by accident."""
    prompt = render_profile_prompt(build_profile(build_report("testuser", [])))

    assert "## Ratings" not in prompt
    assert "## Recent form" not in prompt
    assert "## Trend" not in prompt
    assert "## Repertoire" not in prompt
    assert "## Recurring error patterns" not in prompt


# What _PROFILE_INSTRUCTIONS actually asks for today: third person to a
# coach (v1 said "you", which the embed then read as addressing the
# coach), the unit spelled rather than "ACPL" (the acronym is defined by
# _profile_intro's glossary, which this text outlives -- it is stored and
# pasted where no glossary follows it), and 3-5 sentences plus a short
# bullet list. The blank line and bullets are the point of the shape:
# they put `_blockquote`'s multi-line and empty-line branches in the
# snapshot, where a one-line narrative left them invisible.
_PROFILE_NARRATIVE = (
    "This student favors solid structures as White with the London "
    "System and presses the Pirc as Black, but their results dip "
    "sharply against the Englund Gambit, scoring only 40% there. They "
    "lose far more in the opening than later on -- about 1.30 pawns a "
    "move over the opening phase against 0.32 in the middlegame -- "
    "which points at preparation rather than calculation. Their last "
    "30 days run worse than their whole-span average, 1.34 pawns a "
    "move against 1.07, so the trend is the wrong way.\n"
    "\n"
    "Weaknesses:\n"
    "- Back-rank vulnerability, a third of their blunders.\n"
    "- Walking into a forced mate, another third."
)


def test_render_profile_context_matches_snapshot_with_narrative() -> None:
    profile = build_profile(build_report("testuser", scenario_games())).model_copy(
        update={"narrative": _PROFILE_NARRATIVE}
    )
    context = render_profile_context(profile)

    assert context == render_profile_context(profile), (
        "render_profile_context is not deterministic"
    )
    assert "Coach's read" in context
    quoted = context.split("Coach's read:\n", 1)[1]
    assert [line.lstrip("> ").rstrip() for line in quoted.splitlines()] == [
        line.rstrip() for line in _PROFILE_NARRATIVE.splitlines()
    ]
    assert all(line.startswith(">") for line in quoted.splitlines())
    write_or_check(PROFILE_CONTEXT_SNAPSHOT, context)


def test_render_profile_context_matches_snapshot_without_narrative() -> None:
    """Total over narrative=None (docs/06-coach.md): renders the facts
    alone, with no "Coach's read" section at all."""
    profile = build_profile(build_report("testuser", scenario_games()))
    assert profile.narrative is None

    context = render_profile_context(profile)

    assert "Coach's read" not in context
    write_or_check(PROFILE_CONTEXT_NO_NARRATIVE_SNAPSHOT, context)


def test_render_profile_context_empty_profile_renders_facts_alone() -> None:
    context = render_profile_context(build_profile(build_report("testuser", [])))

    assert context.startswith("## Student profile")
    assert "Coach's read" not in context


def test_profile_context_facts_state_the_unit_never_the_acronym() -> None:
    """Every figure in the block is in pawns, so the block may not call
    one "ACPL" -- the acronym expands to average *centipawn* loss, and
    the hosts it embeds into ask for pawns explicitly. Asserted over the
    facts alone: the narrative is model-written text, produced under a
    prompt whose header does define the acronym."""
    context = render_profile_context(
        build_profile(build_report("testuser", scenario_games()))
    )

    assert "ACPL" not in context
    assert "pawns lost per move" in context


def test_profile_context_quotes_the_narrative_around_any_heading() -> None:
    """Nothing forbids the narrative a markdown heading, so the block
    quotes it: an unquoted "## Tendencies" would forge a section
    boundary and hand the host's later sections to the narrative."""
    profile = build_profile(build_report("testuser", scenario_games())).model_copy(
        update={"narrative": "## Tendencies\n\nSolid as White.\n- Back rank"}
    )

    context = render_profile_context(profile)
    after_read = context.split("Coach's read:\n", 1)[1]

    assert after_read == "> ## Tendencies\n>\n> Solid as White.\n> - Back rank"
    assert [line for line in context.splitlines() if line.startswith("#")] == [
        "## Student profile -- testuser, games (all time controls)"
    ]


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


def test_system_prompt_names_no_one_artifact() -> None:
    """docs/06-coach.md, "Providers": one provider, one system prompt,
    three artifacts behind it -- the report brief, the profile narrative
    and a move explanation. Naming one of them tells the model to
    produce that one whichever call is actually running; what to write
    is each instruction block's own business.
    """
    lowered = SYSTEM_PROMPT.lower()

    for artifact in ("brief", "narrative", "explanation", "explain"):
        assert artifact not in lowered, f"system prompt names the {artifact}"
    assert "instruction block at the end" in lowered


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
    """docs/archive/fixes-2026-07/04-report-engine-tool.md: given an analyst,
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


def test_render_explain_prompt_profile_none_is_byte_identical_to_default() -> None:
    """docs/06-coach.md: with `profile=None` (the default), the prompt
    renders exactly as it always did."""
    lines = [EvalLine(multipv=1, depth=18, eval_cp=35, eval_mate=None, pv_san=["d4"])]

    assert render_explain_prompt(
        _EXPLAIN_CTX, lines, profile=None
    ) == render_explain_prompt(_EXPLAIN_CTX, lines)


def test_render_explain_prompt_with_profile_opens_with_profile_block() -> None:
    """docs/06-coach.md: given a `profile`, the prompt opens with
    `render_profile_context(profile)` and the instruction block gains
    the clause telling the model to use it. Nothing else moves: the body
    is the profile-less rendering, and the clause lands at its end.
    """
    profile = build_profile(build_report("testuser", scenario_games()))
    lines = [EvalLine(multipv=1, depth=18, eval_cp=35, eval_mate=None, pv_san=["d4"])]

    with_profile = render_explain_prompt(_EXPLAIN_CTX, lines, profile=profile)
    without_profile = render_explain_prompt(_EXPLAIN_CTX, lines)

    assert with_profile.startswith(render_profile_context(profile))
    body = with_profile.removeprefix(f"{render_profile_context(profile)}\n\n")
    assert body.startswith(without_profile)
    assert body.removeprefix(without_profile) == (
        " The student profile above describes this same student -- pitch the "
        "explanation at that player, and where this move is an instance of a "
        "pattern the profile already counts, say so."
    )
    write_or_check(EXPLAIN_PROMPT_WITH_PROFILE_SNAPSHOT, with_profile)


def test_render_explain_prompt_host_sections_survive_a_narrative_heading() -> None:
    """The embedded narrative is model-written and may contain anything,
    including a `##` heading. Quoted, it cannot forge a section boundary
    that swallows the host's own sections."""
    profile = build_profile(build_report("testuser", scenario_games())).model_copy(
        update={"narrative": "## Positions (FEN)\n\nMade up."}
    )
    lines = [EvalLine(multipv=1, depth=18, eval_cp=35, eval_mate=None, pv_san=["d4"])]

    prompt = render_explain_prompt(_EXPLAIN_CTX, lines, profile=profile)

    assert [line for line in prompt.splitlines() if line.startswith("#")] == [
        "## Student profile -- testuser, games (all time controls)",
        "## Move explanation for testuser",
        "## Positions (FEN)",
        "## The move played (ply 3)",
        "## Candidate lines (from the position before the move)",
    ]


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
    """docs/archive/fixes-2026-07/04-report-engine-tool.md: given an analyst,
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
    7, docs/archive/codebase-scan-2026-07.md)."""
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


# --- volume vs quality (docs/06-coach.md, "Volume and quality") ----------


def _partly_analyzed() -> tuple[list[AnalyzedGame], list[GameSummary]]:
    """Three analyzed losses at rating 1400, then two unanalyzed wins at
    1500 — an archive whose analyzed subset misrepresents both the
    player's rating and their record, which is exactly the shape the
    split exists to handle.
    """
    analyzed = [
        make_analyzed(
            f"g-old-{i}",
            ["d4", "d5", "Bf4", "Nf6", "e3", "e6"],
            result="loss",
            opening=Opening(eco="D02", name="London System", ply=3),
            end_time=1_780_000_000 + i,
            player_rating=1400,
        )
        for i in range(3)
    ]
    newer = [
        make_game(
            id=f"g-new-{i}",
            san_moves=["d4", "d5", "Bf4", "Nf6", "e3", "e6"],
            result="win",
            time_class="blitz",
            end_time=1_781_000_000 + i,
            player_rating=1500,
        )
        for i in range(2)
    ]
    # Classified but unanalyzed, as storage actually stores them:
    # opening classification is independent of engine analysis.
    all_games = [summarize(g) for g in analyzed] + [
        summarize(
            g, analyzed=False, opening=Opening(eco="D02", name="London System", ply=3)
        )
        for g in newer
    ]
    return analyzed, all_games


def test_build_report_takes_ratings_and_record_from_every_game() -> None:
    """The bug the split fixes: with only the analyzed list, "current
    rating" is whichever game the engine happened to reach last, and the
    win/loss record is a biased subsample."""
    analyzed, all_games = _partly_analyzed()

    biased = build_report("testuser", analyzed)
    correct = build_report("testuser", analyzed, all_games=all_games)

    assert biased.record.games == 3
    assert biased.record.wins == 0
    assert biased.time_classes[0].rating_end == 1400

    assert correct.record.games == 5
    assert correct.record.wins == 2  # the two unanalyzed wins now count
    assert correct.time_classes[0].rating_end == 1500  # the real latest rating


def test_build_report_keeps_quality_over_the_analyzed_subset() -> None:
    """The other half of the contract: nothing an engine produces may be
    diluted by games it never saw."""
    analyzed, all_games = _partly_analyzed()

    correct = build_report("testuser", analyzed, all_games=all_games)
    quality_only = build_report("testuser", analyzed)

    assert correct.games_analyzed == 3
    assert correct.player_moves == quality_only.player_moves
    assert correct.overall_acpl == quality_only.overall_acpl
    assert correct.judgment_counts == quality_only.judgment_counts
    assert correct.phases == quality_only.phases


def test_opening_stats_separates_games_from_analyzed_games() -> None:
    """`OpeningStats.analyzed_games` has always been declared as "how many
    of `games` have engine analysis"; before the split the two were the
    same number by construction."""
    analyzed, all_games = _partly_analyzed()

    (row,) = build_report("testuser", analyzed, all_games=all_games).openings

    assert row.games == 5  # every game in the family
    assert row.analyzed_games == 3  # the ACPL columns' own sample
    assert row.wins == 2  # score reflects all five, not the analyzed three


def test_build_report_without_all_games_is_unchanged() -> None:
    """The default must stay byte-identical for callers that genuinely
    only have analyzed games, which is why `all_games` is optional."""
    analyzed, _ = _partly_analyzed()

    report = build_report("testuser", analyzed)

    assert report.record.games == report.games_analyzed
    assert all(o.games == o.analyzed_games for o in report.openings)


def test_month_volume_counts_every_game_but_acpl_only_analyzed() -> None:
    analyzed, all_games = _partly_analyzed()

    months = {
        m.month: m
        for m in build_report("testuser", analyzed, all_games=all_games).months
    }

    unanalyzed_month = months["2026-06"]
    assert unanalyzed_month.games == 2  # both played
    assert unanalyzed_month.acpl is None  # neither analyzed -- absent, not 0.0
    assert unanalyzed_month.blunder_rate is None


# --- recent form (docs/06-coach.md, "Recent form") -----------------------


def test_periods_are_nested_trailing_windows_narrowest_first() -> None:
    report = build_report("testuser", scenario_games())

    labels = [p.label for p in report.periods]
    assert labels[-1] == "whole span"
    assert labels[:-1] == ["last 30 days"]  # 90d would restate the span
    # Nested, so each wider window contains the narrower one.
    assert report.periods[0].games <= report.periods[-1].games


def test_periods_are_anchored_to_the_newest_game_not_the_clock() -> None:
    """A player who stopped three months ago still gets a recent-form
    read; anchoring to `now` would hand them empty windows instead."""
    ancient = [
        make_analyzed(
            f"g-{i}",
            ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
            end_time=1_600_000_000 + i * 86_400 * 20,
        )
        for i in range(6)
    ]

    periods = build_report("testuser", ancient).periods

    recent = next(p for p in periods if p.days == 30)
    assert recent.games > 0
    assert recent.analyzed_games > 0


def test_period_windows_that_would_restate_the_whole_span_are_dropped() -> None:
    """Two games a day apart make "last 30 days" and "whole span" the
    same row; showing both invites a narrative to read a difference that
    cannot exist."""
    short = [
        make_analyzed(f"g-{i}", ["e4", "e5"], end_time=1_780_000_000 + i * 86_400)
        for i in range(2)
    ]

    assert [p.label for p in build_report("testuser", short).periods] == ["whole span"]


def test_period_with_games_but_no_analysis_reports_absent_quality() -> None:
    analyzed, all_games = _partly_analyzed()

    whole = next(
        p
        for p in build_report("testuser", analyzed, all_games=all_games).periods
        if p.days is None
    )

    assert whole.games == 5
    assert whole.analyzed_games == 3
    assert whole.acpl is not None  # some analysis exists in this window


# --- profile scope (docs/06-coach.md, "Player profile") ------------------


def test_build_profile_carries_the_reports_time_class_and_scope_count() -> None:
    analyzed, all_games = _partly_analyzed()
    report = build_report(
        "testuser",
        analyzed,
        all_games=all_games,
        time_class="blitz",
        games_in_scope=len(all_games),
    )

    profile = build_profile(report)

    assert profile.time_class == "blitz"
    assert profile.games_covered == 3  # analyzed — the quality denominator
    assert profile.games_in_scope == 5  # every game behind the volume figures
    assert profile.periods == report.periods


def test_profile_prompt_states_the_time_control_and_both_denominators() -> None:
    analyzed, all_games = _partly_analyzed()
    profile = build_profile(
        build_report(
            "testuser",
            analyzed,
            all_games=all_games,
            time_class="blitz",
            games_in_scope=len(all_games),
        )
    )

    prompt = render_profile_prompt(profile)

    assert "their blitz games" in prompt
    assert "3 of 5 analyzed" in prompt


def test_profile_context_block_names_its_student_and_scope() -> None:
    """The embed path's whole risk: a rapid profile pasted into a prompt
    with no scope line reads as a description of the whole player. The
    name is there because this header is the first line of the host
    prompt, where a bare "their" refers to nobody yet."""
    analyzed, all_games = _partly_analyzed()
    profile = build_profile(
        build_report("testuser", analyzed, all_games=all_games, time_class="blitz")
    )

    context = render_profile_context(profile)

    assert context.startswith("## Student profile -- testuser, blitz games")
    assert "their" not in context.splitlines()[0]


def test_profile_context_states_coverage_only_when_it_is_partial() -> None:
    analyzed, all_games = _partly_analyzed()
    partial = build_profile(
        build_report(
            "testuser", analyzed, all_games=all_games, games_in_scope=len(all_games)
        )
    )
    complete = build_profile(build_report("testuser", analyzed))

    assert "3 of 5 games analyzed" in render_profile_context(partial)
    assert "Coverage:" not in render_profile_context(complete)


# --- milestones (docs/06-coach.md, "Milestones") -------------------------
#
# Every aggregate below is volume-layer, so each test hands `build_report`
# an *empty* analyzed list beside a full `all_games` — the shape a
# barely-analyzed archive has, and the one that would catch any of these
# being computed over the analyzed subset instead.

_HOUR = 3_600
_DAY = 86_400


def _volume(
    game_id: str,
    *,
    result: Result = "win",
    end_time: int,
    player_rating: int = 1500,
    opponent_rating: int = 1500,
    opponent: str = "rival",
    color: Color = "white",
    time_class: TimeClass = "blitz",
    termination: str | None = None,
) -> GameSummary:
    """One stored, *unanalyzed* game as the volume layer sees it."""
    return summarize(
        make_game(
            id=game_id,
            result=result,
            end_time=end_time,
            player_rating=player_rating,
            opponent_rating=opponent_rating,
            opponent=opponent,
            color=color,
            time_class=time_class,
            termination=termination,
        ),
        analyzed=False,
    )


def _volume_report(games: list[GameSummary]) -> PlayerReport:
    return build_report("testuser", [], all_games=games, games_in_scope=len(games))


def test_time_class_stats_date_the_rating_extremes() -> None:
    """ "Peaked at 1600" is trivia; "peaked at 1600 in March and has been
    below it since" is the fact a student measures themselves against."""
    peak_day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", end_time=peak_day - 10 * _DAY, player_rating=1480),
            _volume("g-2", end_time=peak_day, player_rating=1600),
            _volume("g-3", end_time=peak_day + 10 * _DAY, player_rating=1520),
        ]
    )

    stats = report.time_classes[0]

    assert stats.rating_max == 1600
    assert stats.rating_max_at == peak_day
    assert stats.rating_min == 1480
    assert stats.rating_min_at == peak_day - 10 * _DAY
    assert stats.rating_end == 1520  # still below the peak


def test_rating_peak_is_dated_at_the_first_game_that_reached_it() -> None:
    """A peak is when they got there, not the last time they matched it:
    "peaked in March and has not passed it since" is only true if the
    date is the first."""
    first = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", end_time=first, player_rating=1600),
            _volume("g-2", end_time=first + _DAY, player_rating=1550),
            _volume("g-3", end_time=first + 2 * _DAY, player_rating=1600),
        ]
    )

    assert report.time_classes[0].rating_max_at == first


def test_best_win_is_the_strongest_opponent_actually_beaten() -> None:
    """Losses to stronger players are not milestones, and the winner is
    picked on the opponent's rating, not the player's own."""
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume(
                "g-lost",
                result="loss",
                end_time=day,
                opponent="titled",
                opponent_rating=2200,
            ),
            _volume(
                "g-best",
                result="win",
                end_time=day + _DAY,
                opponent="strongest",
                opponent_rating=1750,
                player_rating=1500,
            ),
            _volume(
                "g-ok",
                result="win",
                end_time=day + 2 * _DAY,
                opponent="weaker",
                opponent_rating=1400,
            ),
        ]
    )

    assert report.best_win is not None
    assert report.best_win.game_id == "g-best"
    assert report.best_win.opponent == "strongest"
    assert report.best_win.opponent_rating == 1750
    assert report.best_win.player_rating == 1500


def test_best_win_is_none_without_a_win() -> None:
    report = _volume_report([_volume("g-1", result="loss", end_time=1_780_000_000)])

    assert report.best_win is None


def test_streaks_report_the_current_run_and_the_longest_ones() -> None:
    day = 1_780_000_000
    results: list[Result] = ["win", "win", "win", "loss", "draw", "loss", "loss"]
    report = _volume_report(
        [
            _volume(f"g-{i}", result=result, end_time=day + i * _DAY)
            for i, result in enumerate(results)
        ]
    )

    assert report.streaks is not None
    assert report.streaks.current_result == "loss"
    assert report.streaks.current_length == 2  # the two most recent games
    assert report.streaks.longest_win == 3
    assert report.streaks.longest_loss == 2


def test_after_loss_counts_the_next_game_of_the_same_sitting() -> None:
    """Tilt: the game played straight after a loss. Chained losses each
    seed the next game, and a game played the following day does not
    count — that is a fresh sitting, not a rebound."""
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", result="loss", end_time=day),
            _volume("g-2", result="loss", end_time=day + _HOUR),  # after a loss
            _volume("g-3", result="win", end_time=day + 2 * _HOUR),  # after a loss
            _volume("g-4", result="win", end_time=day + 3 * _DAY),  # new sitting
        ]
    )

    assert report.streaks is not None
    assert report.streaks.after_loss.games == 2
    assert report.streaks.after_loss.wins == 1
    assert report.streaks.after_loss.losses == 1


def test_after_loss_is_an_empty_record_when_no_game_follows_a_loss() -> None:
    """An empty sample, never a 0% score: on an archive of one game a
    day, "scores 0% after a loss" would be a fabrication."""
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", result="loss", end_time=day),
            _volume("g-2", result="win", end_time=day + _DAY),
        ]
    )

    assert report.streaks is not None
    assert report.streaks.after_loss.games == 0


def test_color_records_split_the_score_by_side() -> None:
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", result="win", color="white", end_time=day),
            _volume("g-2", result="win", color="white", end_time=day + _DAY),
            _volume("g-3", result="loss", color="black", end_time=day + 2 * _DAY),
        ]
    )

    assert report.color_records["white"].wins == 2
    assert report.color_records["black"].games == 1
    assert report.color_records["black"].wins == 0


def test_color_records_keep_a_side_the_player_never_had() -> None:
    """Zero games reads as "no sample"; a missing key would make every
    consumer guess."""
    report = _volume_report([_volume("g-1", color="white", end_time=1_780_000_000)])

    assert report.color_records["black"].games == 0


def test_milestones_cover_every_game_not_just_the_analyzed_ones() -> None:
    """The bug the volume/quality split exists to stamp out, one level
    down: a best win the engine never reached is still a best win."""
    analyzed, all_games = _partly_analyzed()

    biased = build_report("testuser", analyzed)
    correct = build_report("testuser", analyzed, all_games=all_games)

    assert biased.best_win is None  # every analyzed game is a loss
    assert correct.best_win is not None
    assert correct.streaks is not None
    assert correct.streaks.current_result == "win"


def test_report_prompt_renders_recent_form_and_milestones() -> None:
    """The gap this closes: the brief never rendered `periods` at all,
    so every piece of advice averaged the whole span flat while the
    profile narrative beside it led with the last 30 days."""
    report = build_report("testuser", scenario_games())

    prompt = render_prompt(report)

    assert "## Recent form" in prompt
    assert "| last 30 days |" in prompt
    assert "## Milestones" in prompt
    assert "- Best win: beat " in prompt
    assert "- By color: White " in prompt
    assert "**Recent form first.**" in prompt


def test_report_milestone_lines_state_no_subject() -> None:
    """The brief's *headings* are second person because the advice is
    written to the student, but its data lines are label:value and
    subject-free. An assertion like "you beat marko77" reads against a
    system prompt that opens "You are a ... coach", so the referent is
    briefly ambiguous where a label:value line has no referent at all
    (docs/06-coach.md, "Milestones")."""
    report = build_report("testuser", scenario_games())

    section = render_prompt(report).split("## Milestones")[1].split("\n\n")[0]

    for pronoun in ("you ", "your ", "you'", "their "):
        assert pronoun not in section.lower()


def test_report_prompt_names_the_best_win_opponent() -> None:
    """The opposite of the profile prompt's rule, for the opposite
    reason: this brief's citation rule is "name the game by opponent
    and date", and it is read by the student who played it."""
    report = build_report("testuser", scenario_games())

    prompt = render_prompt(report)

    assert report.best_win is not None
    assert report.best_win.opponent in prompt


def test_report_prompt_and_report_chat_seed_show_the_same_sections() -> None:
    """docs/06-coach.md pins the report-scope chat seed as "the same data
    sections render_prompt shows, minus the instruction block" -- a new
    section must reach both or that promise quietly rots."""
    report = build_report("testuser", scenario_games())

    prompt = render_prompt(report)
    seed = render_report_chat_context(report, engine_available=True)

    for heading in ("## Recent form", "## Milestones", "## How games end"):
        assert heading in prompt
        assert heading in seed


def test_build_profile_copies_the_milestones_verbatim() -> None:
    """The profile restates the report's milestones; it re-derives
    none of them (docs/06-coach.md, "Milestones")."""
    report = build_report("testuser", scenario_games())

    profile = build_profile(report)

    assert profile.record == report.record
    assert profile.best_win == report.best_win
    assert profile.streaks == report.streaks
    assert profile.color_records == report.color_records
    assert profile.opponents == report.opponents
    assert profile.terminations == report.terminations


def test_profile_prompt_reads_the_after_loss_score_against_the_overall_one() -> None:
    """The figure means nothing alone: 50% after a loss is bad only next
    to a better overall score, so the comparison is rendered, not left
    for the model to find."""
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", result="win", end_time=day),
            _volume("g-2", result="win", end_time=day + _DAY),
            _volume("g-3", result="loss", end_time=day + 2 * _DAY),
            _volume("g-4", result="loss", end_time=day + 2 * _DAY + _HOUR),
        ]
    )

    prompt = render_profile_prompt(build_profile(report))

    assert "- After a loss: 0% (1g) in the next game of the same sitting" in prompt
    assert "against 50% (4g) overall" in prompt


def test_profile_prompt_renders_the_dated_peak_and_the_milestones() -> None:
    report = build_report("testuser", scenario_games())

    prompt = render_profile_prompt(build_profile(report))

    peak = report.time_classes[0]
    assert peak.rating_max_at is not None
    assert f"{peak.rating_max} on " in prompt
    assert "## Milestones and tendencies" in prompt
    assert "- Best win: beat a " in prompt
    assert "## How games end" in prompt


def test_profile_prompt_best_win_names_no_opponent() -> None:
    """The narrative's citation ban applies to the facts it is given: an
    opponent's handle is a game reference, and this text is embedded
    where no reference can be resolved (docs/06-coach.md, "Narrative")."""
    report = _volume_report(
        [
            _volume(
                "g-1",
                result="win",
                end_time=1_780_000_000,
                opponent="marko77",
                opponent_rating=1700,
            )
        ]
    )

    prompt = render_profile_prompt(build_profile(report))

    assert "1700" in prompt
    assert "marko77" not in prompt


def test_profile_context_states_how_the_student_loses() -> None:
    """The one milestone the embedded block carries: "winning, then lost
    on time" is a different lesson from "winning, then blundered"."""
    day = 1_780_000_000
    report = _volume_report(
        [
            _volume("g-1", result="loss", end_time=day, termination="timeout"),
            _volume("g-2", result="loss", end_time=day + _DAY, termination="timeout"),
            _volume(
                "g-3", result="loss", end_time=day + 2 * _DAY, termination="resigned"
            ),
        ]
    )

    context = render_profile_context(build_profile(report))

    assert "- How they lose: 3 losses -- timeout 67%, resigned 33%" in context


def test_profile_context_omits_the_losing_line_with_a_single_cause() -> None:
    """One code restates the record above it, exactly as the report's own
    terminations section collapses a single-code result."""
    day = 1_780_000_000
    report = _volume_report(
        [_volume("g-1", result="loss", end_time=day, termination="resigned")]
    )

    assert "How they lose" not in render_profile_context(build_profile(report))
