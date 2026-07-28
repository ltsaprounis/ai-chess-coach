"""Render prompts for the coach component (docs/06-coach.md).

Both `render_prompt` (the full-report coaching prompt) and
`render_explain_prompt` (the move-explanation prompt) are deterministic
and user-visible (the UI shows them with a copy button), so changes here
are effectively UI changes too.
"""

import re
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
PROMPT_VERSION = "2026-07-game-links"

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
    'repertoire lists it under their color in "Systems you chose". Never '
    'advise dropping a line from the "What you face" table -- recommend '
    "a response to it instead.\n"
    "- **Citation.** Refer to positions by date and move number, "
    "written as a markdown reference link through the entry's `cite` "
    'handle -- e.g. "[your 26...Nb6 in the June 14 blitz game][g3]" -- '
    "never a raw URL, never an invented handle, never a list position "
    "or table row. Every mention of a handled position should cite "
    "this way.\n"
    "- **One biggest lever.** Open with the single change most likely to "
    "raise this student's results, not a flat list of co-equal "
    "weaknesses. Order everything else by impact behind it.\n"
    "- **Honesty.** If the data does not support a conclusion -- too few "
    "games, no sample past the floor -- say so plainly instead of "
    "filling the section anyway.\n"
    "- **Verification.** When the `analyze_position` tool is available: "
    "for each turning point the brief features, run the tool on that "
    "entry's FEN and state the refutation -- what the played move loses "
    "to, not just the better move's name -- and check any other concrete "
    "line before asserting it. Never present an unverified variation as "
    "fact.\n"
    "- **Plan.** Close with a two-week training plan sized to the time "
    "controls and volume shown above, not a generic study list."
)


def render_prompt(report: PlayerReport) -> str:
    # One handle assignment, shared by both sections that cite a position
    # and by `append_game_links` after the model has run (docs/06-coach.md,
    # "Game links") -- so a position can never be numbered two different
    # ways depending on who is asking.
    handles = _game_link_handles(report)
    sections = [
        _student_section(report),
        _phase_section(report),
        _trend_section(report),
        _terminations_section(report),
        _repertoire_section(report),
        _error_patterns_section(report, handles),
        _turning_points_section(report, handles),
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
    requested = _requested_window_line(report)
    if requested:
        lines.append(requested)
    window = _window_line(report)
    if window:
        lines.append(window)
    lines.extend(_coverage_lines(report))
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


def _requested_window_line(report: PlayerReport) -> str | None:
    """The window the caller asked for, alongside the covered span above
    (docs/06-coach.md, "Coverage is stated, not implied"). Renders
    whenever either bound is present, handling a one-sided request (only
    `since`, or only `until`) gracefully; `None` throughout renders
    nothing, keeping scope-free reports byte-identical to before.
    """
    since = report.requested_since
    until = report.requested_until
    if since is None and until is None:
        return None
    if since is None:
        assert until is not None  # narrowed: not (since is None and until is None)
        return f"- Requested: until {_format_date(until)}"
    if until is None:
        return f"- Requested: since {_format_date(since)}"
    return f"- Requested: {_format_date(since)} to {_format_date(until)}"


def _coverage_lines(report: PlayerReport) -> list[str]:
    """Coverage as N of M games in scope, with an explicit caveat when
    analysis covers less than the requested scope -- the statement that
    lets the instruction block's honesty rule actually bite
    (docs/06-coach.md). `games_in_scope is None` (the caller supplied no
    scope info) renders nothing.
    """
    if report.games_in_scope is None:
        return []
    verb = "is" if report.games_analyzed == 1 else "are"
    lines = [
        f"- Coverage: {report.games_analyzed} of "
        f"{_plural(report.games_in_scope, 'game')} in scope {verb} analyzed"
    ]
    missing = report.games_in_scope - report.games_analyzed
    if missing > 0:
        verb = "is" if missing == 1 else "are"
        lines.append(
            f"- Note: the other {_plural(missing, 'game')} in scope "
            f"{verb} not engine-analyzed; every figure below describes "
            "only the analyzed span."
        )
    return lines


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
class _FamilyRecord:
    """Fields shared by both rollup partitions -- enough for impact/score."""

    games: int
    wins: int
    losses: int
    draws: int
    opening_acpl: float | None
    avg_cp_loss: float | None


@dataclass
class _Family(_FamilyRecord):
    """A chosen-partition family: one (color, system) rolled up."""

    label: str
    system: str
    first_moves: str


@dataclass
class _FacedFamily(_FamilyRecord):
    """A faced-partition family: one (color, name root) rolled up.

    No `system` -- for faced lines the name is the opponent's choice, and
    the player's own reply (hence `system`) varies member to member, so
    there is no single system to show (docs/06-coach.md, "Family
    rollup").
    """

    label: str
    first_moves: str


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
    """Two sub-tables per color: rows partition by `faced` before any
    rollup (docs/06-coach.md, "Rendering the split"). The chosen partition
    is the player's repertoire; the faced partition is the coaching
    target for "learn a response", never "stop playing this". Below-floor
    families from *both* partitions fold into one shared long-tail line.
    """
    total_games = sum(r.games for r in rows)
    chosen_families = _rollup_chosen_families([r for r in rows if not r.faced])
    faced_families = _rollup_faced_families([r for r in rows if r.faced])

    chosen_main = [f for f in chosen_families if f.games >= _REPERTOIRE_SAMPLE_FLOOR]
    chosen_tail = [f for f in chosen_families if f.games < _REPERTOIRE_SAMPLE_FLOOR]
    faced_main = [f for f in faced_families if f.games >= _REPERTOIRE_SAMPLE_FLOOR]
    faced_tail = [f for f in faced_families if f.games < _REPERTOIRE_SAMPLE_FLOOR]
    chosen_main.sort(key=lambda f: -_family_impact(f))
    faced_main.sort(key=lambda f: -_family_impact(f))

    parts = [
        f"### As {label} ({_plural(total_games, 'game')})",
        _chosen_subtable(chosen_main),
        _faced_subtable(faced_main, label),
    ]
    tail: list[_FamilyRecord] = [*chosen_tail, *faced_tail]
    if tail:
        tail_games = sum(f.games for f in tail)
        parts.append(
            f"Long tail: {_plural(len(tail), 'line')} under "
            f"{_REPERTOIRE_SAMPLE_FLOOR} games, {_plural(tail_games, 'game')} total."
        )
    return "\n".join(parts)


def _chosen_subtable(families: list[_Family]) -> str:
    lines = ["#### Systems you chose"]
    if not families:
        lines.append(
            f"No line yet reaches the {_REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
        )
        return "\n".join(lines)
    lines += [
        "| System (first moves) | Games | Score | Opening ACPL | Game ACPL |",
        "|---|---|---|---|---|",
    ]
    for f in families:
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
    return "\n".join(lines)


def _faced_subtable(families: list[_FacedFamily], label: str) -> str:
    lines = [f"#### What you face as {label}"]
    if not families:
        lines.append(
            f"No line yet reaches the {_REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
        )
        return "\n".join(lines)
    lines += [
        "| Opponent's line (your reply) | Games | Score | Opening ACPL | Game ACPL |",
        "|---|---|---|---|---|",
    ]
    for f in families:
        # No `system` column here -- the name is the opponent's choice,
        # and `first_moves` alone already shows both the opponent's line
        # and the player's own reply to it.
        lines.append(
            f"| {f.label} ({f.first_moves}) | {f.games} "
            f"| {_family_score(f)}% | {_pawns_or_na(f.opening_acpl)} "
            f"| {_pawns_or_na(f.avg_cp_loss)} |"
        )
    return "\n".join(lines)


def _rollup_chosen_families(rows: list[OpeningStats]) -> list[_Family]:
    """Collapse the chosen partition by (color, system) -- rows already
    share one color and are pre-filtered to `not faced`.

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


def _rollup_faced_families(rows: list[OpeningStats]) -> list[_FacedFamily]:
    """Collapse the faced partition by (color, name root) -- rows already
    share one color and are pre-filtered to `faced`.

    For faced lines the name *is* the opponent's choice, while the
    player's own system varies with their replies, so keying on `system`
    (as the chosen partition does) would split one opposing gambit across
    as many families as the player has tried answers to it. Summing and
    the move-weighted ACPL re-weighting are otherwise identical to
    `_rollup_chosen_families` -- only the key differs (docs/06-coach.md,
    "Family rollup").
    """
    groups: dict[str, list[OpeningStats]] = defaultdict(list)
    for row in rows:
        groups[row.name.split(":")[0].strip()].append(row)

    families: list[_FacedFamily] = []
    for label, members in groups.items():
        lead = min(members, key=lambda r: (-r.games, r.eco, r.name))
        families.append(
            _FacedFamily(
                label=label,
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


def _family_impact(f: _FamilyRecord) -> float:
    score = (f.wins + f.draws / 2) / f.games if f.games else 0.0
    return f.games * (0.5 - score)


def _family_score(f: _FamilyRecord) -> str:
    score = (f.wins + f.draws / 2) / f.games if f.games else 0.0
    return f"{score * 100:.0f}"


# --- error patterns ------------------------------------------------------


def _error_patterns_section(
    report: PlayerReport, handles: dict[tuple[str, int], str]
) -> str:
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
            f"| {_error_example(e, handles)} |"
        )
    return "\n".join(lines)


def _error_example(e: ErrorPattern, handles: dict[tuple[str, int], str]) -> str:
    """Date and move number, with side, plus a `(cite [gN])` handle so the
    model can cite this instance the same way it cites a turning point
    (docs/06-coach.md, "Game links") -- a bare game id/ply is exactly the
    unfindable handle the citation rule bans, and a plain date and move
    number alone gives the model nothing it is allowed to link through.
    The "n/a" path (an example field missing) carries no handle -- there
    is nothing to cite."""
    if (
        e.example_game_id is None
        or e.example_end_time is None
        or e.example_move_number is None
        or e.example_ply is None
    ):
        return "n/a"
    date = _format_date(e.example_end_time)
    # Name the side in words rather than with SAN's trailing "."/"...",
    # which needs a move after it to read as notation instead of as a
    # typo. Turning points can use the glyphs; this cell has no move.
    side = "White" if e.example_ply % 2 == 1 else "Black"
    base = f"{date}, {side}'s move {e.example_move_number}"
    handle = handles.get((e.example_game_id, e.example_ply))
    return f"{base} (cite [{handle}])" if handle else base


# --- turning points -------------------------------------------------------


def _turning_points_section(
    report: PlayerReport, handles: dict[tuple[str, int], str]
) -> str:
    if not report.critical_positions:
        return ""
    lines = ["## Turning points"]
    for n, p in enumerate(report.critical_positions, start=1):
        lines.append(_turning_point_entry(n, p, handles[(p.game_id, p.ply)]))
    return "\n".join(lines)


def _turning_point_entry(n: int, p: CriticalPosition, handle: str) -> str:
    date = _format_date(p.end_time)
    color_word = "White" if p.color == "white" else "Black"
    opening = f", {p.opening_name}" if p.opening_name else ""
    move_label = f"{p.move_number}." if p.color == "white" else f"{p.move_number}..."
    lines = [
        f"### {n}. {date}, {p.time_class}, as {color_word}{opening} "
        f"-- move {p.move_number} -- cite [{handle}]"
    ]
    if p.leading_up:
        lines.append(f"Leading up: {' '.join(p.leading_up)}")
    lines.append(f"FEN: `{p.fen}`")
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


# --- game links (docs/06-coach.md, "Game links") --------------------------
#
# Citations must survive the trip through the model without it ever
# writing a URL -- game ids are UUID-plus-username strings, and one
# mistyped character is a broken link. `_game_link_handles` is the one
# source of truth for the `[g1]`, `[g2]`, ... assignment; `render_prompt`
# renders each handle visibly next to the position it names, and
# `append_game_links` (run on the model's advice afterwards) is the only
# place a handle ever turns into a real URL.


def _game_link_handles(report: PlayerReport) -> dict[tuple[str, int], str]:
    """Assign `g1`, `g2`, ... to every distinct `(game_id, ply)` a citable
    position points at -- turning points first, in render order, then
    error-pattern examples that carry one. An error example landing on
    the exact position a turning point already names reuses that turning
    point's handle rather than minting a second one for the same place.
    """
    handles: dict[tuple[str, int], str] = {}
    for p in report.critical_positions:
        handles.setdefault((p.game_id, p.ply), f"g{len(handles) + 1}")
    for e in report.error_patterns:
        if e.example_game_id is None or e.example_ply is None:
            continue
        handles.setdefault((e.example_game_id, e.example_ply), f"g{len(handles) + 1}")
    return handles


# Matches an inline-link slip `[text](gN)` -- the model reaching for the
# more familiar inline markdown form instead of the reference form the
# instructions ask for. Always rewritten to reference form, offered
# handle or not -- the offered/unknown split happens once, in
# `_HANDLE_REFERENCE`'s pass below, so an inline slip through an
# unoffered handle degrades exactly like a reference citation through
# one would (docs/06-coach.md: "an invented handle renders as its text,
# in inline or reference form alike"). Link text is assumed
# bracket-free, which every citation this prompt asks for is.
_INLINE_HANDLE_SLIP = re.compile(r"\[([^\[\]]*)\]\((g\d+)\)")
# Matches a reference-style citation `[text][gN]`, offered or not -- the
# offered/unknown split happens in `_degrade_unknown`, not in the regex.
_HANDLE_REFERENCE = re.compile(r"\[([^\[\]]*)\]\[(g\d+)\]")
# Matches a model-authored reference *definition* line for a handle --
# `[gN]: whatever`, at line start (allowing the up-to-3-space indent
# CommonMark itself allows for a definition). CommonMark resolves a
# repeated definition to the *first* one in document order, so an
# unstripped line here would let the model's own line win against the
# minted definition appended below and point a handle anywhere it
# likes -- stripped unconditionally, offered handle or not.
_MODEL_HANDLE_DEFINITION = re.compile(r"^ {0,3}\[g\d+\]:[^\n]*\n?", re.MULTILINE)


def append_game_links(advice: str, report: PlayerReport) -> str:
    """Post-process the model's advice so its handle citations resolve.

    Three passes always run, even when the report has no citable games at
    all: strip any model-authored `[gN]:` definition line (a hijack
    attempt -- CommonMark lets the *first* definition of a label win, so
    an unstripped one could redirect a handle anywhere), normalize an
    inline `[text](gN)` slip to the reference form, and degrade a
    citation through a handle the prompt never offered to its plain text,
    in either form (an invented handle is exactly as unfindable as no
    citation at all; with no citable games every handle is "unknown", so
    every citation the model still wrote degrades). Only the last step is
    conditional: appending one `[gN]: /games/{id}?ply={n}` reference
    definition per offered handle -- markdown renders an unused
    definition as nothing, so appending every offered handle is free, but
    there is nothing to append when there are none. URLs are minted here
    from the report; the model never writes one.
    """
    handles = _game_link_handles(report)
    offered = set(handles.values())

    def _degrade_unknown(match: re.Match[str]) -> str:
        label, handle = match.group(1), match.group(2)
        return match.group(0) if handle in offered else label

    text = _MODEL_HANDLE_DEFINITION.sub("", advice)
    text = _INLINE_HANDLE_SLIP.sub(r"[\1][\2]", text)
    text = _HANDLE_REFERENCE.sub(_degrade_unknown, text)

    if not handles:
        return text

    definitions = "\n".join(
        f"[{handle}]: /games/{game_id}?ply={ply}"
        for (game_id, ply), handle in handles.items()
    )
    return "\n\n".join([text, definitions])


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
