"""Coach component tests (docs/06-coach.md)."""

from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

import chess_coach.coach.providers as providers_module
from chess_coach.coach import (
    CoachProviderError,
    build_report,
    create_provider,
    render_prompt,
)
from chess_coach.domain import (
    AnalyzedGame,
    GameAnalysis,
    LlmConfig,
    MoveEval,
    Opening,
)
from tests.factories import make_analysis, make_game

RUY = Opening(eco="C60", name="Ruy Lopez", ply=5)


def analyzed(
    game_id: str,
    *,
    result: str = "win",
    opening: Opening | None = RUY,
    analysis: GameAnalysis | None = None,
) -> AnalyzedGame:
    game = make_game(id=game_id, result=result)
    return AnalyzedGame.model_validate(
        {
            **game.model_dump(),
            "opening": opening,
            "analysis": analysis or make_analysis(game_id=game_id),
        }
    )


def test_build_report_aggregates_player_stats() -> None:
    report = build_report(
        "testuser",
        [
            analyzed("g-1", result="win"),
            analyzed("g-2", result="loss", opening=None),
        ],
    )

    assert report.username == "testuser"
    assert report.games_analyzed == 2
    assert report.overall_acpl == 2.5  # both factory analyses are 2.5
    assert report.acpl_by_phase["opening"] == 2.5
    assert report.judgment_counts["best"] == 2
    # Only the classified game contributes to the repertoire.
    assert [(s.eco, s.games, s.wins) for s in report.openings] == [("C60", 1, 1)]
    assert report.openings[0].avg_cp_loss == 2.5


def test_openings_sorted_worst_first() -> None:
    games = [
        analyzed("w1", result="win"),
        analyzed("w2", result="win"),
        analyzed(
            "l1",
            result="loss",
            opening=Opening(eco="D06", name="Queen's Gambit", ply=3),
        ),
    ]
    report = build_report("testuser", games)
    assert [s.eco for s in report.openings] == ["D06", "C60"]


def test_critical_positions_replay_to_fen() -> None:
    # White's third move (Nf3, index 2 -> ply 3) loses 300 cp; the
    # position before it is after 1. e4 e5.
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
    game = make_game(id="g-crit", san_moves=["e4", "e5", "Nf3"], color="white")
    report = build_report(
        "testuser",
        [
            AnalyzedGame.model_validate(
                {**game.model_dump(), "opening": None, "analysis": analysis}
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


def test_render_prompt_is_deterministic_and_complete() -> None:
    # Give the analysis a player loss so a critical position exists.
    evals = [
        MoveEval(
            ply=1,
            san="e4",
            eval_cp=-270,
            eval_mate=None,
            best_move="d2d4",
            cp_loss=300,
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-1").model_copy(update={"evals": evals})
    report = build_report("testuser", [analyzed("g-1", analysis=analysis)])
    prompt = render_prompt(report)

    assert prompt == render_prompt(report)
    assert "## Player profile: testuser" in prompt
    assert "| C60 | Ruy Lopez | 1 | 1-0-0 | 2.5 |" in prompt
    assert "## Costliest moves" in prompt
    assert "played e4 (lost 300 cp; engine preferred d4)" in prompt
    assert "Training plan" in prompt


def test_mate_scale_losses_render_as_words_not_centipawns() -> None:
    evals = [
        MoveEval(
            ply=1,
            san="f3",
            eval_cp=None,
            eval_mate=-2,
            best_move="e2e4",
            cp_loss=10_050,  # walked into a forced mate
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-mate").model_copy(update={"evals": evals})
    report = build_report("testuser", [analyzed("g-mate", analysis=analysis)])
    prompt = render_prompt(report)

    assert "forced-mate-scale blunder" in prompt
    assert "10050" not in prompt  # no nonsense centipawn numbers


def test_empty_report_prompt_has_no_empty_sections() -> None:
    prompt = render_prompt(build_report("testuser", []))
    assert "Repertoire" not in prompt
    assert "Costliest" not in prompt


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
    from claude_agent_sdk import ResultMessage

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


def test_unimplemented_providers_raise_clearly() -> None:
    with pytest.raises(CoachProviderError, match="not implemented"):
        create_provider(LlmConfig(provider="anthropic"))
