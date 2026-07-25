"""Render prompts for the coach component (docs/06-coach.md).

Both `render_prompt` (the full-report coaching prompt) and
`render_explain_prompt` (the move-explanation prompt) are deterministic
and user-visible (the UI shows them with a copy button), so changes here
are effectively UI changes too.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from chess_coach.coach.context import MoveContext
from chess_coach.domain import (
    MATE_SCORE,
    CriticalPosition,
    ErrorPattern,
    EvalLine,
    OpeningStats,
    PlayerReport,
    Record,
)

# Bumped whenever the template changes materially -- the API layer keys
# its report cache on this, so a reworded prompt invalidates cached advice
# instead of being served alongside a template that no longer exists.
PROMPT_VERSION = "2026-07-rework"

# Given to the LLM as its system prompt -- it replaces the Claude Code
# coding persona when running through the Agent SDK provider.
SYSTEM_PROMPT = (
    "You are a strong, practical chess coach reviewing a student's "
    "engine-analyzed games. Every figure in the brief below is already "
    "move-weighted and carries its own denominator -- read the numbers as "
    "given rather than recomputing or re-averaging them. This is a "
    "coaching conversation, not a software task: respond with the "
    "coaching brief only, no preamble about the nature of the request, "
    "and follow the instruction block at the end exactly."
)

# Losses this large can only come from mate scores (evals clamp mate to
# +/-MATE_SCORE); render them as words, not nonsense centipawns.
_MATE_SCALE = MATE_SCORE - 1_000

_REPERTOIRE_SAMPLE_FLOOR = 5

_INSTRUCTIONS = (
    "## Instructions\n"
    "Write the coaching brief now, following these rules:\n"
    "- **Audience and register.** Write for a club player, not a fellow "
    "engine: pawns, never centipawns, and lead with the idea -- the "
    "threat, the plan, what a line wins -- before any number.\n"
    "- **Attribution.** An opening is the student's own only where the "
    "repertoire lists it under their color as a system they chose. Never "
    "advise dropping an opening they only face as the other side -- "
    "recommend a response to it instead.\n"
    "- **Citation.** Refer to positions and games by date and move "
    'number (e.g. "your 26...Nb6 in the June 14 blitz game"), never by '
    "list position or table row.\n"
    "- **One biggest lever.** Open with the single change most likely to "
    "raise this student's results, not a flat list of co-equal "
    "weaknesses. Order everything else by impact behind it.\n"
    "- **Honesty.** If the data does not support a conclusion -- too few "
    "games, no sample past the floor -- say so plainly instead of "
    "filling the section anyway.\n"
    "- **Plan.** Close with a two-week training plan sized to the time "
    "controls and volume shown above, not a generic study list."
)


def render_prompt(report: PlayerReport) -> str:
    sections = [
        _student_section(report),
        _phase_section(report),
        _trend_section(report),
        _terminations_section(report),
        _repertoire_section(report),
        _error_patterns_section(report),
        _turning_points_section(report),
        _INSTRUCTIONS,
    ]
    return "\n\n".join(section for section in sections if section)


# --- the student ------------------------------------------------------


def _student_section(report: PlayerReport) -> str:
    lines = [
        f"# Coaching brief -- {report.username}",
        "*(ACPL = average centipawn loss per move, shown in pawns; lower "
        "is better. Every figure below is move-weighted across the games "
        "in scope.)*",
        "",
        "## The student",
    ]
    window = _window_line(report)
    if window:
        lines.append(window)
    lines.append(
        f"- Scope: {report.time_class} only"
        if report.time_class
        else "- Scope: all time controls"
    )
    lines.append(f"- Analyzed: {_plural(report.games_analyzed, 'game')}")
    for tc in report.time_classes:
        lines.append(
            f"- {tc.time_class.capitalize()}: {_plural(tc.record.games, 'game')}, "
            f"rating {tc.rating_start} → {tc.rating_end} "
            f"(range {tc.rating_min}-{tc.rating_max})"
        )
    if report.opponents:
        o = report.opponents
        lines.append(
            f"- Opposition: avg rating diff {o.avg_rating_diff:+.0f}; "
            f"{_score_line(o.vs_stronger)} vs stronger, "
            f"{_score_line(o.vs_similar)} vs similar, "
            f"{_score_line(o.vs_weaker)} vs weaker"
        )
    return "\n".join(lines)


def _window_line(report: PlayerReport) -> str | None:
    if report.window_start is None or report.window_end is None:
        return None
    start = _format_date(report.window_start)
    end = _format_date(report.window_end)
    return f"- Window: {start} to {end}"


def _score_line(record: Record) -> str:
    if not record.games:
        return "n/a"
    score = (record.wins + record.draws / 2) / record.games
    return f"{score * 100:.0f}% ({record.games}g)"


# --- phase breakdown and judgment rates --------------------------------


def _phase_section(report: PlayerReport) -> str:
    lines = [
        "## How the play breaks down",
        "| Phase | Moves | ACPL | Blunder % |",
        "|---|---|---|---|",
    ]
    for phase in ("opening", "middlegame", "endgame"):
        stats = report.phases.get(phase)
        if stats is None:
            continue
        acpl = _pawns_or_na(stats.acpl)
        blunder_pct = _rate(stats.judgment_counts.get("blunder", 0), stats.moves)
        lines.append(
            f"| {phase.capitalize()} | {stats.moves} | {acpl} | {blunder_pct} |"
        )

    counts = report.judgment_counts
    total_moves = report.player_moves
    quality = ", ".join(
        f"{j} {_rate(counts.get(j, 0), total_moves)} ({counts.get(j, 0)})"
        for j in ("best", "good", "inaccuracy", "mistake", "blunder")
    )
    blunders_per_game = (
        round(counts.get("blunder", 0) / report.games_analyzed, 1)
        if report.games_analyzed
        else 0.0
    )
    lines.append(
        f"Overall {_pawns_or_na(report.overall_acpl)} ACPL over {total_moves} "
        f"moves -- {quality}; {blunders_per_game} blunders/game."
    )
    return "\n".join(lines)


def _rate(count: int, denom: int) -> str:
    return f"{count / denom * 100:.1f}%" if denom else "n/a"


# --- trend, terminations ------------------------------------------------


def _trend_section(report: PlayerReport) -> str:
    if not report.months:
        return ""
    lines = [
        "## Trend",
        "| Month | Games | Rating | ACPL | Blunder % |",
        "|---|---|---|---|---|",
    ]
    for m in report.months:
        rating = str(m.rating_end) if m.rating_end is not None else "n/a"
        acpl = _pawns_or_na(m.acpl)
        blunder_pct = (
            f"{m.blunder_rate * 100:.1f}%" if m.blunder_rate is not None else "n/a"
        )
        lines.append(f"| {m.month} | {m.games} | {rating} | {acpl} | {blunder_pct} |")
    return "\n".join(lines)


def _terminations_section(report: PlayerReport) -> str:
    if not report.terminations:
        return ""
    lines = ["## How games end"]
    labels = {"win": "Wins", "loss": "Losses", "draw": "Draws"}
    # Losses first: this is the one section built to say "38% of your
    # losses are on the clock", and that signal lives in losses and draws
    # -- never in wins. chess.com's per-player result code for the
    # winning side is always the literal string "win"
    # (ingestion/normalize.py), so a win row can never have more than one
    # distinct termination.
    for result in ("loss", "draw", "win"):
        rows = [t for t in report.terminations if t.result == result]
        if not rows:
            continue
        total = sum(t.games for t in rows)
        if len(rows) == 1:
            # One code can't discriminate anything -- it always equals
            # the total already stated, so rendering it as "<code> <n>"
            # (e.g. the "Wins 9: win 9" bug) is pure noise.
            lines.append(f"{labels[result]} {total}")
            continue
        detail = ", ".join(f"{t.termination} {t.games}" for t in rows)
        lines.append(f"{labels[result]} {total}: {detail}")
    return "\n".join(lines)


# --- repertoire, split by color -----------------------------------------


@dataclass
class _Family:
    label: str
    system: str
    first_moves: str
    games: int
    wins: int
    losses: int
    draws: int
    opening_acpl: float | None
    avg_cp_loss: float | None


def _repertoire_section(report: PlayerReport) -> str:
    white = [o for o in report.openings if o.color == "white"]
    black = [o for o in report.openings if o.color == "black"]
    if not white and not black:
        return ""
    parts = ["## Repertoire"]
    if white:
        parts.append(_repertoire_color_section("White", white))
    if black:
        parts.append(_repertoire_color_section("Black", black))
    return "\n\n".join(parts)


def _repertoire_color_section(label: str, rows: list[OpeningStats]) -> str:
    families = _rollup_families(rows)
    total_games = sum(f.games for f in families)
    main = [f for f in families if f.games >= _REPERTOIRE_SAMPLE_FLOOR]
    tail = [f for f in families if f.games < _REPERTOIRE_SAMPLE_FLOOR]
    main.sort(key=lambda f: -_family_impact(f))

    lines = [f"### As {label} ({_plural(total_games, 'game')})"]
    if main:
        lines += [
            "| System (first moves) | Games | Score | Opening ACPL | Game ACPL |",
            "|---|---|---|---|---|",
        ]
        for f in main:
            # `system` (the student's own moves) is rendered explicitly, not
            # just implied by `first_moves` -- an opponent's reply can make
            # an otherwise-unremarkable system read like a named gambit
            # (the Englund regression), so the student's own choice must be
            # legible on its own, not just inferable from the full line.
            lines.append(
                f"| {f.label} -- {f.system} ({f.first_moves}) | {f.games} "
                f"| {_family_score(f)}% | {_pawns_or_na(f.opening_acpl)} "
                f"| {_pawns_or_na(f.avg_cp_loss)} |"
            )
    else:
        lines.append(
            f"No line yet reaches the {_REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
        )
    if tail:
        tail_games = sum(f.games for f in tail)
        lines.append(
            f"Long tail: {_plural(len(tail), 'line')} under "
            f"{_REPERTOIRE_SAMPLE_FLOOR} games, {_plural(tail_games, 'game')} total."
        )
    return "\n".join(lines)


def _rollup_families(rows: list[OpeningStats]) -> list[_Family]:
    """Collapse rows by (color, system) -- rows already share one color.

    Labels the family with its most-played member's name root (the name
    up to the first colon); ties broken by games, then eco, then name for
    determinism. Both ACPL columns re-weight by moves, not by games:
    `opening_acpl` by each row's `opening_moves`, `avg_cp_loss` by its
    `player_moves` -- the exact denominators `OpeningStats` carries for
    this reason. Weighting by game count instead would rebuild the
    mean-of-per-game-means one level above the row it was just removed
    from (docs/06-coach.md, "Family rollup").
    """
    groups: dict[str, list[OpeningStats]] = defaultdict(list)
    for row in rows:
        groups[row.system].append(row)

    families: list[_Family] = []
    for system, members in groups.items():
        lead = min(members, key=lambda r: (-r.games, r.eco, r.name))
        families.append(
            _Family(
                label=lead.name.split(":")[0].strip(),
                system=system,
                first_moves=lead.first_moves,
                games=sum(r.games for r in members),
                wins=sum(r.wins for r in members),
                losses=sum(r.losses for r in members),
                draws=sum(r.draws for r in members),
                opening_acpl=_weighted_mean(
                    [
                        (r.opening_acpl, r.opening_moves)
                        for r in members
                        if r.opening_acpl is not None
                    ]
                ),
                avg_cp_loss=_weighted_mean(
                    [
                        (r.avg_cp_loss, r.player_moves)
                        for r in members
                        if r.avg_cp_loss is not None
                    ]
                ),
            )
        )
    return families


def _weighted_mean(pairs: list[tuple[float, int]]) -> float | None:
    total_weight = sum(w for _, w in pairs)
    if not total_weight:
        return None
    return round(sum(v * w for v, w in pairs) / total_weight, 1)


def _family_impact(f: _Family) -> float:
    score = (f.wins + f.draws / 2) / f.games if f.games else 0.0
    return f.games * (0.5 - score)


def _family_score(f: _Family) -> str:
    score = (f.wins + f.draws / 2) / f.games if f.games else 0.0
    return f"{score * 100:.0f}"


# --- error patterns ------------------------------------------------------


def _error_patterns_section(report: PlayerReport) -> str:
    if not report.error_patterns:
        return ""
    lines = [
        "## Recurring error patterns",
        "| Pattern | Count | % of blunders | Example |",
        "|---|---|---|---|",
    ]
    for e in report.error_patterns:
        lines.append(
            f"| {e.label} | {e.count} | {e.share_of_blunders * 100:.1f}% "
            f"| {_error_example(e)} |"
        )
    return "\n".join(lines)


def _error_example(e: ErrorPattern) -> str:
    """Date and move number, with side -- the same citation rule turning
    points already follow. A bare game id/ply is exactly the unfindable
    handle the citation rule (docs/06-coach.md) bans."""
    if (
        e.example_end_time is None
        or e.example_move_number is None
        or e.example_ply is None
    ):
        return "n/a"
    date = _format_date(e.example_end_time)
    # Name the side in words rather than with SAN's trailing "."/"...",
    # which needs a move after it to read as notation instead of as a
    # typo. Turning points can use the glyphs; this cell has no move.
    side = "White" if e.example_ply % 2 == 1 else "Black"
    return f"{date}, {side}'s move {e.example_move_number}"


# --- turning points -------------------------------------------------------


def _turning_points_section(report: PlayerReport) -> str:
    if not report.critical_positions:
        return ""
    lines = ["## Turning points"]
    for n, p in enumerate(report.critical_positions, start=1):
        lines.append(_turning_point_entry(n, p))
    return "\n".join(lines)


def _turning_point_entry(n: int, p: CriticalPosition) -> str:
    date = _format_date(p.end_time)
    color_word = "White" if p.color == "white" else "Black"
    opening = f", {p.opening_name}" if p.opening_name else ""
    move_label = f"{p.move_number}." if p.color == "white" else f"{p.move_number}..."
    lines = [
        f"### {n}. {date}, {p.time_class}, as {color_word}{opening} "
        f"-- move {p.move_number}"
    ]
    if p.leading_up:
        lines.append(f"Leading up: {' '.join(p.leading_up)}")
    swing = (
        f"{format_eval(p.eval_before_cp, p.eval_before_mate)} to "
        f"{format_eval(p.eval_after_cp, p.eval_after_mate)}"
    )
    # Turning points are never mate-scale by construction (report.py
    # excludes them from candidacy), so cp_loss always renders as pawns.
    lines.append(
        f"You played **{move_label}{p.played}** (lost {format_cp_loss(p.cp_loss)}): "
        f"{swing}. Engine preferred **{p.best}**."
    )
    return "\n".join(lines)


# --- shared formatting helpers -------------------------------------------


def _format_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _pawns_or_na(value: float | None) -> str:
    """A bare pawns magnitude for a table cell -- never raw centipawns.

    `format_cp_loss`/`format_eval` below cover narrative sentences and
    signed evals respectively; neither fits an unsigned table number, so
    this extends the same "pawns never cp" rule to that last shape.
    """
    return f"{value / 100:.2f}" if value is not None else "n/a"


# How many of a candidate line's moves to show -- enough to read the plan,
# short enough to keep the table scannable.
_PV_MOVES_SHOWN = 5

# Style contract (docs/06-coach.md): club-player audience, pawns never
# centipawns, idea before number, no redundant "?"/"??"-plus-judgment-word
# annotation. Keep the tool-use and concise-and-concrete instructions.
_EXPLAIN_INSTRUCTIONS = (
    "Explain why the played move loses to the engine's best move above, "
    "for a club player -- not a fellow engine. Lead with the idea: the "
    "threat, the plan, or what the refutation wins; bring in numbers only "
    'as support. Give every evaluation swing in pawns ("about 4 pawns"), '
    'never centipawns. Skip engine-style annotation -- no "?"/"??" next '
    "to a move you're also calling a mistake or blunder; say it once, in "
    "plain language. Use the `analyze_position` tool for follow-ups -- for "
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
