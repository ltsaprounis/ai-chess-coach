"""Coach component — see docs/06-coach.md."""

from chess_coach.coach.comparisons import COMPARISON_FDR, build_comparisons
from chess_coach.coach.context import MoveContext, build_move_context
from chess_coach.coach.highlights import (
    HighlightMove,
    PlayerHighlights,
    build_highlights,
)
from chess_coach.coach.level import (
    WINDOW_DRIFT_POINTS,
    WINDOW_MAX_MONTHS,
    WINDOW_MIN_GAMES,
    build_trajectory,
    profile_window,
    window_spans_level_change,
)
from chess_coach.coach.profile import build_profile
from chess_coach.coach.prompt import (
    PROFILE_PROMPT_VERSION,
    PROMPT_VERSION,
    append_game_links,
    render_chat_prompt,
    render_explain_prompt,
    render_game_chat_context,
    render_profile_context,
    render_profile_prompt,
    render_prompt,
    render_report_chat_context,
)
from chess_coach.coach.providers import (
    ChatEvent,
    ChatToolkit,
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
    "COMPARISON_FDR",
    "PROFILE_PROMPT_VERSION",
    "PROMPT_VERSION",
    "WINDOW_DRIFT_POINTS",
    "WINDOW_MAX_MONTHS",
    "WINDOW_MIN_GAMES",
    "ChatEvent",
    "ChatToolkit",
    "ClaudeAgentSdkProvider",
    "CoachProvider",
    "CoachProviderError",
    "CopilotSdkProvider",
    "ExplainEvent",
    "HighlightMove",
    "MoveContext",
    "PlayerHighlights",
    "PositionAnalystFn",
    "append_game_links",
    "build_comparisons",
    "build_highlights",
    "build_move_context",
    "build_profile",
    "build_report",
    "build_trajectory",
    "create_provider",
    "profile_window",
    "render_chat_prompt",
    "render_explain_prompt",
    "render_game_chat_context",
    "render_profile_context",
    "render_profile_prompt",
    "render_prompt",
    "render_report_chat_context",
    "window_spans_level_change",
]
