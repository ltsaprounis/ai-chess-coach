"""LLM providers behind the CoachProvider seam (docs/06-coach.md)."""

import logging
from typing import Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from chess_coach.coach.prompt import SYSTEM_PROMPT
from chess_coach.domain import LlmConfig

logger = logging.getLogger(__name__)


class CoachProviderError(Exception):
    """The provider could not produce advice."""


class CoachProvider(Protocol):
    async def complete(self, prompt: str) -> str: ...


class ClaudeAgentSdkProvider:
    """One-shot completion through the local Claude Code login.

    No API key anywhere: authentication and billing ride the user's
    Claude subscription. Requires the `claude` CLI to be installed
    and logged in on this machine.
    """

    def __init__(self, model: str, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt

    async def complete(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self._model,
            max_turns=1,
            system_prompt=self._system_prompt,
        )
        chunks: list[str] = []
        fallback: str | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                elif isinstance(message, ResultMessage):
                    fallback = message.result
                    if message.is_error:
                        # The actionable detail (e.g. "Not logged in")
                        # arrives in `result`, not `subtype`.
                        detail = message.result or message.subtype
                        raise CoachProviderError(
                            f"claude-agent-sdk run failed: {detail}"
                        )
                    if message.total_cost_usd is not None:
                        logger.info(
                            "coach completion: %.4f USD, %d turn(s)",
                            message.total_cost_usd,
                            message.num_turns,
                        )
        except CoachProviderError:
            raise
        except Exception as exc:  # CLI missing, process death, transport
            raise CoachProviderError(
                f"claude-agent-sdk failed: {exc} — is the `claude` CLI "
                "installed and logged in?"
            ) from exc

        text = "".join(chunks).strip() or (fallback or "").strip()
        if not text:
            raise CoachProviderError("claude-agent-sdk returned no text")
        return text


def create_provider(cfg: LlmConfig, api_key: str | None = None) -> CoachProvider:
    """Factory over the seam; only claude-agent-sdk ships today.

    `api_key` is reserved for the anthropic / azure-foundry providers.
    """
    if cfg.provider == "claude-agent-sdk":
        return ClaudeAgentSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    raise CoachProviderError(
        f"llm provider {cfg.provider!r} is not implemented yet — "
        "set llm.provider to 'claude-agent-sdk'"
    )
