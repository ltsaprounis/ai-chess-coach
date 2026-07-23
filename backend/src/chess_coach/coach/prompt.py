"""Render prompts for the coach component (docs/06-coach.md).

Both `render_prompt` (the full-report coaching prompt) and
`render_explain_prompt` (the move-explanation prompt) are deterministic
and user-visible (the UI shows them with a copy button), so changes here
are effectively UI changes too.
"""

from chess_coach.coach.context import MoveContext
from chess_coach.domain import MATE_SCORE, EvalLine, PlayerReport

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


# How many of a candidate line's moves to show — enough to read the plan,
# short enough to keep the table scannable.
_PV_MOVES_SHOWN = 5

# Style contract (docs/06-coach.md): club-player audience, pawns never
# centipawns, idea before number, no redundant "?"/"??"-plus-judgment-word
# annotation. Keep the tool-use and concise-and-concrete instructions.
_EXPLAIN_INSTRUCTIONS = (
    "Explain why the played move loses to the engine's best move above, "
    "for a club player — not a fellow engine. Lead with the idea: the "
    "threat, the plan, or what the refutation wins; bring in numbers only "
    'as support. Give every evaluation swing in pawns ("about 4 pawns"), '
    'never centipawns. Skip engine-style annotation — no "?"/"??" next '
    "to a move you're also calling a mistake or blunder; say it once, in "
    "plain language. Use the `analyze_position` tool for follow-ups — for "
    "example, analyze the position after the move (the second FEN above) "
    "to name the opponent's refutation. Keep the explanation concise and "
    "concrete."
)


def render_explain_prompt(ctx: MoveContext, lines: list[EvalLine]) -> str:
    sections = [
        _explain_intro(ctx),
        _explain_positions(ctx),
        _explain_move(ctx),
        _explain_lines(lines),
        _EXPLAIN_INSTRUCTIONS,
    ]
    return "\n\n".join(section for section in sections if section)


def _explain_intro(ctx: MoveContext) -> str:
    opening = f" in a {ctx.opening_name} game" if ctx.opening_name else ""
    return (
        f"## Move explanation for {ctx.username}\n"
        f"{ctx.username} was playing {ctx.color}{opening}."
    )


def _explain_positions(ctx: MoveContext) -> str:
    return (
        "## Positions (FEN)\n"
        f"- Before the move: `{ctx.fen_before}`\n"
        f"- After the move: `{ctx.fen_after}`"
    )


def _explain_move(ctx: MoveContext) -> str:
    if ctx.cp_loss >= _MATE_SCALE:
        cost = "walked into a forced mate"
    else:
        cost = f"lost {format_cp_loss(ctx.cp_loss)}"
    return (
        f"## The move played (ply {ctx.ply})\n"
        f"{ctx.username} played **{ctx.san}** ({cost}; judged **{ctx.judgment}**), "
        f"instead of the engine's preferred **{ctx.best_move}**."
    )


def _explain_lines(lines: list[EvalLine]) -> str:
    if not lines:
        return ""
    rows = [
        "## Candidate lines (from the position before the move)",
        "| Rank | Depth | Eval | Line |",
        "|------|-------|------|------|",
    ]
    for line in lines:
        pv = " ".join(line.pv_san[:_PV_MOVES_SHOWN])
        if len(line.pv_san) > _PV_MOVES_SHOWN:
            pv += " …"
        rows.append(
            f"| {line.multipv} | {line.depth} "
            f"| {format_eval(line.eval_cp, line.eval_mate)} | {pv} |"
        )
    return "\n".join(rows)


def format_eval(eval_cp: int | None, eval_mate: int | None) -> str:
    """Render one engine score as short, human-readable text (white's POV)."""
    if eval_mate is not None:
        if eval_mate > 0:
            return f"White mates in {eval_mate}"
        return f"Black mates in {-eval_mate}"
    if eval_cp is not None:
        return f"{eval_cp / 100:+.2f}"
    return "n/a"


def format_cp_loss(cp_loss: int) -> str:
    """Render a centipawn-loss magnitude in pawns — never raw centipawns.

    Extends format_eval's pawn treatment to loss magnitudes, so
    render_explain_prompt never hands the model a bare cp number (e.g.
    421) it might parrot back as engine jargon instead of writing for a
    club player.
    """
    return f"about {cp_loss / 100:.1f} pawns"
