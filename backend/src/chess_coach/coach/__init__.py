"""Coach component — see docs/06-coach.md."""

from chess_coach.coach.context import MoveContext, build_move_context
from chess_coach.coach.prompt import render_explain_prompt, render_prompt
from chess_coach.coach.providers import (
    ClaudeAgentSdkProvider,
    CoachProvider,
    CoachProviderError,
    CopilotSdkProvider,
    ExplainEvent,
    PositionAnalystFn,
    create_provider,
)
from chess_coach.coach.report import build_report

__all__ = [
    "ClaudeAgentSdkProvider",
    "CoachProvider",
    "CoachProviderError",
    "CopilotSdkProvider",
    "ExplainEvent",
    "MoveContext",
    "PositionAnalystFn",
    "build_move_context",
    "build_report",
    "create_provider",
    "render_explain_prompt",
    "render_prompt",
]
