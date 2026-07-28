"""Coach component — see docs/06-coach.md."""

from chess_coach.coach.context import MoveContext, build_move_context
from chess_coach.coach.highlights import (
    HighlightMove,
    PlayerHighlights,
    build_highlights,
)
from chess_coach.coach.prompt import (
    PROMPT_VERSION,
    render_explain_prompt,
    render_prompt,
)
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
    "PROMPT_VERSION",
    "ClaudeAgentSdkProvider",
    "CoachProvider",
    "CoachProviderError",
    "CopilotSdkProvider",
    "ExplainEvent",
    "HighlightMove",
    "MoveContext",
    "PlayerHighlights",
    "PositionAnalystFn",
    "build_highlights",
    "build_move_context",
    "build_report",
    "create_provider",
    "render_explain_prompt",
    "render_prompt",
]
