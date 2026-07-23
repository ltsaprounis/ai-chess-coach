"""Render a PlayerReport into the coaching prompt (docs/06-coach.md).

The template is deterministic and user-visible (the UI shows it with
a copy button), so changes here are effectively UI changes too.
"""

from chess_coach.domain import MATE_SCORE, PlayerReport

# Given to the LLM as its system prompt — it replaces the Claude Code
# coding persona when running through the Agent SDK provider.
SYSTEM_PROMPT = (
    "You are a strong, practical chess coach reviewing a student's "
    "engine-analyzed games. This is a coaching conversation, not a "
    "software task: respond with the coaching report only, no "
    "preamble about the nature of the request."
)

_ROLE = (
    "You are a strong, practical chess coach. Below is aggregated "
    "Stockfish analysis of your student's recent chess.com games. "
    "Centipawn loss (cp) measures how much each move worsened their "
    "position; ACPL is the average per move (lower is better)."
)

# Losses this large can only come from mate scores (evals clamp mate
# to ±MATE_SCORE); render them as words, not nonsense centipawns.
_MATE_SCALE = MATE_SCORE - 1_000

_INSTRUCTIONS = (
    "Coach this player. Respond in markdown with exactly these "
    "sections:\n"
    "1. **Overall assessment** — two or three sentences on their play.\n"
    "2. **Top three weaknesses** — concrete, evidence-based, citing "
    "the numbers or positions above.\n"
    "3. **Opening advice** — what to keep, drop, or study, based on "
    "the repertoire table.\n"
    "4. **Training plan** — three specific, actionable exercises for "
    "the next two weeks.\n"
    "Be direct and specific; avoid generic advice."
)


def render_prompt(report: PlayerReport) -> str:
    sections = [
        _ROLE,
        _profile(report),
        _repertoire(report),
        _critical(report),
        _INSTRUCTIONS,
    ]
    return "\n\n".join(section for section in sections if section)


def _profile(report: PlayerReport) -> str:
    counts = report.judgment_counts
    lines = [
        f"## Player profile: {report.username}",
        f"- Games analyzed: {report.games_analyzed}",
        f"- Overall ACPL: {report.overall_acpl}",
        "- ACPL by phase: "
        + ", ".join(
            f"{phase} {value}" for phase, value in report.acpl_by_phase.items()
        ),
        "- Move quality: "
        + ", ".join(f"{name} {count}" for name, count in counts.items()),
    ]
    return "\n".join(lines)


def _repertoire(report: PlayerReport) -> str:
    if not report.openings:
        return ""
    lines = [
        "## Repertoire (worst-scoring first)",
        "| ECO | Opening | Games | W-L-D | Avg cp loss |",
        "|-----|---------|-------|-------|-------------|",
    ]
    for s in report.openings:
        lines.append(
            f"| {s.eco} | {s.name} | {s.games} "
            f"| {s.wins}-{s.losses}-{s.draws} | {s.avg_cp_loss} |"
        )
    return "\n".join(lines)


def _critical(report: PlayerReport) -> str:
    if not report.critical_positions:
        return ""
    lines = ["## Costliest moves (position before the move, as FEN)"]
    for n, p in enumerate(report.critical_positions, start=1):
        if p.cp_loss >= _MATE_SCALE:
            cost = "a decisive, forced-mate-scale blunder"
        else:
            cost = f"lost {p.cp_loss} cp"
        lines.append(
            f"{n}. `{p.fen}` — played {p.played} ({cost}; engine preferred {p.best})"
        )
    return "\n".join(lines)
