"""Coach component — see docs/06-coach.md."""

from chess_coach.coach.prompt import render_prompt
from chess_coach.coach.providers import (
    ClaudeAgentSdkProvider,
    CoachProvider,
    CoachProviderError,
    create_provider,
)
from chess_coach.coach.report import build_report

__all__ = [
    "ClaudeAgentSdkProvider",
    "CoachProvider",
    "CoachProviderError",
    "build_report",
    "create_provider",
    "render_prompt",
]
