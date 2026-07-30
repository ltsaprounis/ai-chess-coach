"""Render prompts for the coach component (docs/06-coach.md).

Both `render_prompt` (the full-report coaching prompt) and
`render_explain_prompt` (the move-explanation prompt) are deterministic
and user-visible (the UI shows them with a copy button), so changes here
are effectively UI changes too.
"""

import re
from datetime import UTC, datetime

from chess_coach.coach.context import MoveContext, build_move_context
from chess_coach.coach.repertoire import (
    REPERTOIRE_SAMPLE_FLOOR,
    FacedFamily,
    Family,
    FamilyRecord,
    family_impact,
    family_score,
    rollup_chosen_families,
    rollup_faced_families,
)
from chess_coach.domain import (
    MATE_SCORE,
    ChatMessage,
    CriticalPosition,
    ErrorPattern,
    EvalLine,
    GameDetail,
    MonthStats,
    OpeningStats,
    PlayerProfile,
    PlayerReport,
    ProfileOpening,
    Record,
)

# Bumped whenever the template changes materially -- the API layer keys
# its report cache on this, so a reworded prompt invalidates cached advice
# instead of being served alongside a template that no longer exists.
PROMPT_VERSION = "2026-07-opponent-citations"

# The narrative's own version, independent of PROMPT_VERSION above -- the
# report template and the profile prompt evolve on separate schedules
# (docs/06-coach.md, "Player profile"). Row metadata, never a cache key:
# a bump only flags the stored narrative as stale in the UI and must
# never trigger a silent re-bill.
PROFILE_PROMPT_VERSION = "profile-v1"

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

# Given to the LLM as its system prompt for a chat turn (docs/06-coach.md,
# "Chat") -- SYSTEM_PROMPT above is tailored to writing one-shot briefs
# ("respond with the coaching brief only") and would read oddly telling a
# model to keep doing that mid-conversation, so chat gets its own persona
# line. The scope seed (render_game_chat_context / render_report_chat_context)
# is concatenated after this by the provider and carries the actual
# instructions (_CHAT_INSTRUCTIONS below).
CHAT_SYSTEM_PROMPT = (
    "You are a strong, practical chess coach in a live conversation with "
    "a student about their own engine-analyzed games. This is a coaching "
    "conversation, not a software task: respond conversationally, with "
    "no preamble about the nature of the request, and follow the "
    "instructions in the context below."
)

# Losses this large can only come from mate scores (evals clamp mate to
# +/-MATE_SCORE); render them as words, not nonsense centipawns.
_MATE_SCALE = MATE_SCORE - 1_000

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
    "- **Citation.** Game first, move second: name the game by "
    "opponent and date at its first citation, then give the move in "
    'notation as the link, e.g. "In your game against marko77 on '
    'June 14, [26...Nb6][g3] ...", written through the '
    "entry's `cite` handle. Never a raw URL, never an invented "
    "handle, never a list position or table row. Later references "
    'to an already-cited game may shorten (e.g. "that marko77 '
    'game"). The opening name appears only as coaching content, '
    "never as the identifier; state the time class only when the "
    "report mixes time controls (the student section's scope line "
    "says which) and omit it otherwise.\n"
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
        _trend_section(report.months),
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


def _trend_section(months: list[MonthStats]) -> str:
    """Renders `months` as-is -- shared by the report prompt (its own
    `report.months`) and `render_profile_prompt`, which renders the
    profile's capped `months` through the same table rather than
    growing a second trend format.
    """
    if not months:
        return ""
    lines = [
        "## Trend",
        "| Month | Games | Rating | ACPL | Blunder % |",
        "|---|---|---|---|---|",
    ]
    for m in months:
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
#
# The family rollup itself (partition by `faced`, collapse by key,
# move-weighted sums) lives in `chess_coach.coach.repertoire`, shared
# with `profile.py`'s repertoire rows -- everything below is this
# module's own concern: sample-floor filtering, sorting and markdown
# rendering (docs/06-coach.md, "Family rollup").


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
    chosen_families = rollup_chosen_families([r for r in rows if not r.faced])
    faced_families = rollup_faced_families([r for r in rows if r.faced])

    chosen_main = [f for f in chosen_families if f.games >= REPERTOIRE_SAMPLE_FLOOR]
    chosen_tail = [f for f in chosen_families if f.games < REPERTOIRE_SAMPLE_FLOOR]
    faced_main = [f for f in faced_families if f.games >= REPERTOIRE_SAMPLE_FLOOR]
    faced_tail = [f for f in faced_families if f.games < REPERTOIRE_SAMPLE_FLOOR]
    chosen_main.sort(key=lambda f: -family_impact(f))
    faced_main.sort(key=lambda f: -family_impact(f))

    parts = [
        f"### As {label} ({_plural(total_games, 'game')})",
        _chosen_subtable(chosen_main),
        _faced_subtable(faced_main, label),
    ]
    tail: list[FamilyRecord] = [*chosen_tail, *faced_tail]
    if tail:
        tail_games = sum(f.games for f in tail)
        parts.append(
            f"Long tail: {_plural(len(tail), 'line')} under "
            f"{REPERTOIRE_SAMPLE_FLOOR} games, {_plural(tail_games, 'game')} total."
        )
    return "\n".join(parts)


def _chosen_subtable(families: list[Family]) -> str:
    lines = ["#### Systems you chose"]
    if not families:
        lines.append(
            f"No line yet reaches the {REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
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
            f"| {_family_score_pct(f)}% | {_pawns_or_na(f.opening_acpl)} "
            f"| {_pawns_or_na(f.avg_cp_loss)} |"
        )
    return "\n".join(lines)


def _faced_subtable(families: list[FacedFamily], label: str) -> str:
    lines = [f"#### What you face as {label}"]
    if not families:
        lines.append(
            f"No line yet reaches the {REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
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
            f"| {_family_score_pct(f)}% | {_pawns_or_na(f.opening_acpl)} "
            f"| {_pawns_or_na(f.avg_cp_loss)} |"
        )
    return "\n".join(lines)


def _family_score_pct(f: FamilyRecord) -> str:
    """`family_score` (repertoire.py) as the rounded percent string every
    repertoire table cell renders."""
    return f"{family_score(f) * 100:.0f}"


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
    """Date and opponent, plus move number with side, plus a `(cite [gN])`
    handle so the model can cite this instance the same way it cites a
    turning point (docs/06-coach.md, "Game links") -- a bare game id/ply
    is exactly the unfindable handle the citation rule bans, and a plain
    date and move number alone gives the model nothing it is allowed to
    link through. `example_opponent` is optional for symmetry with the
    other `example_*` fields; when a directly-constructed report leaves
    it None, the cell renders without the "vs ..." clause rather than
    "vs None". The "n/a" path (a required example field missing) carries
    no handle -- there is nothing to cite."""
    if (
        e.example_game_id is None
        or e.example_end_time is None
        or e.example_move_number is None
        or e.example_ply is None
    ):
        return "n/a"
    date = _format_date(e.example_end_time)
    opponent = f" vs {e.example_opponent}" if e.example_opponent else ""
    # Name the side in words rather than with SAN's trailing "."/"...",
    # which needs a move after it to read as notation instead of as a
    # typo. Turning points can use the glyphs; this cell has no move.
    side = "White" if e.example_ply % 2 == 1 else "Black"
    base = f"{date}{opponent}, {side}'s move {e.example_move_number}"
    handle = handles.get((e.example_game_id, e.example_ply))
    return f"{base} (cite [{handle}])" if handle else base


# --- turning points -------------------------------------------------------


def _turning_points_section(
    report: PlayerReport, handles: dict[tuple[str, int], str] | None = None
) -> str:
    """`handles` is optional: `render_prompt`'s coaching brief cites every
    turning point through a `[gN]` handle `append_game_links` later
    resolves, but the chat seed (`render_report_chat_context`) has no such
    post-processing pass (docs/06-coach.md, "Chat" -- "Link discipline" in
    the design record) and renders the same entries with no citation
    handle at all -- `None` (the default) omits the "cite [gN]" suffix.
    """
    if not report.critical_positions:
        return ""
    lines = ["## Turning points"]
    for n, p in enumerate(report.critical_positions, start=1):
        handle = handles.get((p.game_id, p.ply)) if handles is not None else None
        lines.append(_turning_point_entry(n, p, handle))
    return "\n".join(lines)


def _turning_point_entry(n: int, p: CriticalPosition, handle: str | None) -> str:
    date = _format_date(p.end_time)
    color_word = "White" if p.color == "white" else "Black"
    opening = f", {p.opening_name}" if p.opening_name else ""
    move_label = f"{p.move_number}." if p.color == "white" else f"{p.move_number}..."
    cite = f" -- cite [{handle}]" if handle else ""
    lines = [
        f"### {n}. {date}, {p.time_class}, as {color_word} vs {p.opponent}"
        f"{opening} -- move {p.move_number}{cite}"
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


# --- player profile (docs/06-coach.md, "Player profile") -------------------
#
# Two renderers over `PlayerProfile` -- the coach's compact, durable
# distillation of an already-built report (`profile.py:build_profile`).
# `render_profile_prompt` asks an LLM for the narrative layer;
# `render_profile_context` is the ~250-token block `render_explain_prompt`
# and `render_game_chat_context` embed at the top when given a profile.
# Neither renders per-example game identity (unlike the report's
# error-pattern table) and the narrative instructions forbid the model
# from inventing one: this text is stored and reused inside other
# prompts, where a game reference could not be resolved into a link or
# checked by a tool (docs/06-coach.md, "Narrative").


def _profile_intro(profile: PlayerProfile) -> str:
    window = ""
    if profile.window_start is not None and profile.window_end is not None:
        window = (
            f", {_format_date(profile.window_start)} to "
            f"{_format_date(profile.window_end)}"
        )
    return (
        f"# Player profile -- {profile.username}\n"
        "*(ACPL = average centipawn loss per move, shown in pawns; lower "
        "is better. Every figure below is move-weighted over the games "
        "covered.)*\n"
        f"Covered: {_plural(profile.games_covered, 'game')}{window}."
    )


def _profile_ratings_section(profile: PlayerProfile) -> str:
    if not profile.time_classes:
        return ""
    lines = [
        "## Ratings",
        "| Time class | Score | Rating |",
        "|---|---|---|",
    ]
    for tc in profile.time_classes:
        lines.append(
            f"| {tc.time_class.capitalize()} | {_score_line(tc.record)} "
            f"| {tc.rating_start} → {tc.rating_end} "
            f"(range {tc.rating_min}-{tc.rating_max}) |"
        )
    return "\n".join(lines)


def _profile_quality_section(profile: PlayerProfile) -> str:
    """Mirrors `_phase_section`'s shape over `PlayerProfile`'s own field
    names (`games_covered` where the report has `games_analyzed`) -- kept
    as its own function rather than a shared parametrization, since the
    two source types diverge in exactly that one field.
    """
    lines = [
        "## Quality",
        "| Phase | Moves | ACPL | Blunder % |",
        "|---|---|---|---|",
    ]
    for phase in ("opening", "middlegame", "endgame"):
        stats = profile.phases.get(phase)
        if stats is None:
            continue
        acpl = _pawns_or_na(stats.acpl)
        blunder_pct = _rate(stats.judgment_counts.get("blunder", 0), stats.moves)
        lines.append(
            f"| {phase.capitalize()} | {stats.moves} | {acpl} | {blunder_pct} |"
        )

    counts = profile.judgment_counts
    total_moves = profile.player_moves
    quality = ", ".join(
        f"{j} {_rate(counts.get(j, 0), total_moves)} ({counts.get(j, 0)})"
        for j in ("best", "good", "inaccuracy", "mistake", "blunder")
    )
    blunders_per_game = (
        round(counts.get("blunder", 0) / profile.games_covered, 1)
        if profile.games_covered
        else 0.0
    )
    lines.append(
        f"Overall {_pawns_or_na(profile.overall_acpl)} ACPL over {total_moves} "
        f"moves -- {quality}; {blunders_per_game} blunders/game."
    )
    return "\n".join(lines)


def _profile_opening_entry(o: ProfileOpening) -> str:
    return f"{o.name} ({o.moves}) -- {o.games}g, {o.score * 100:.0f}%"


def _profile_repertoire_section(profile: PlayerProfile) -> str:
    if not profile.openings:
        return ""
    parts = ["## Repertoire"]
    for color, label in (("white", "White"), ("black", "Black")):
        rows = [o for o in profile.openings if o.color == color]
        if not rows:
            continue
        parts.append(_profile_repertoire_color_section(label, rows))
    return "\n\n".join(parts)


def _profile_repertoire_color_section(label: str, rows: list[ProfileOpening]) -> str:
    chosen = [o for o in rows if not o.faced]
    faced = [o for o in rows if o.faced]
    lines = [f"### As {label}"]
    if chosen:
        lines.append("Systems you chose:")
        lines.extend(f"- {_profile_opening_entry(o)}" for o in chosen)
    if faced:
        lines.append(f"What you face as {label}:")
        lines.extend(f"- {_profile_opening_entry(o)}" for o in faced)
    return "\n".join(lines)


def _profile_error_patterns_section(profile: PlayerProfile) -> str:
    """Counts and share only -- no example identity, unlike
    `_error_patterns_section` above: the profile prompt offers no game
    citation of any kind (docs/06-coach.md, "Narrative").
    """
    if not profile.error_patterns:
        return ""
    lines = [
        "## Recurring error patterns",
        "| Pattern | Count | % of blunders |",
        "|---|---|---|",
    ]
    for e in profile.error_patterns:
        lines.append(f"| {e.label} | {e.count} | {e.share_of_blunders * 100:.1f}% |")
    return "\n".join(lines)


_PROFILE_INSTRUCTIONS = (
    "## Instructions\n"
    "Write the player's narrative now, following these rules:\n"
    "- **Length and shape.** Three to five sentences describing this "
    "student's tendencies, then a short list of weaknesses -- a handful "
    "of bullets, not an essay.\n"
    "- **Evidence.** Every claim must tie to a figure stated above -- a "
    "rating, an ACPL, a blunder rate, a repertoire score, an "
    "error-pattern count. Never assert a tendency the facts do not "
    "support.\n"
    "- **Audience and register.** Write for a club player, not a fellow "
    "engine: pawns, never centipawns, and address the student directly "
    'as "you".\n'
    "- **No invented lines.** Do not assert a concrete variation, "
    "opening trap, or line of play beyond what the facts state -- these "
    "are aggregates, not annotated games, and there is no engine here "
    "to verify a claimed line.\n"
    "- **No game citations.** Never reference a specific game, date, or "
    "opponent, and never write a link or handle of any kind -- this "
    "text is stored and reused inside other prompts, where a game "
    "reference could not be resolved into a link or checked.\n"
    "- **Honesty.** If a section's sample is too thin to support a "
    "claim, say so or omit it rather than filling space."
)


def render_profile_prompt(profile: PlayerProfile) -> str:
    """The narrative-generation prompt (docs/06-coach.md, "Player
    profile"): the facts -- fuller than `render_profile_context`, e.g. a
    full months table rather than one compact trend line -- followed by
    instructions asking for 3-5 sentences of tendencies plus a short
    weakness list, every claim tied to a figure stated in the facts.
    """
    sections = [
        _profile_intro(profile),
        _profile_ratings_section(profile),
        _profile_quality_section(profile),
        _trend_section(profile.months),
        _profile_repertoire_section(profile),
        _profile_error_patterns_section(profile),
        _PROFILE_INSTRUCTIONS,
    ]
    return "\n\n".join(section for section in sections if section)


def _profile_ratings_line(profile: PlayerProfile) -> str | None:
    if not profile.time_classes:
        return None
    parts = "; ".join(
        f"{tc.time_class.capitalize()} {tc.rating_end} ({tc.record.games}g)"
        for tc in profile.time_classes
    )
    return f"- Ratings: {parts}"


def _profile_quality_line(profile: PlayerProfile) -> str:
    overall_blunder = _rate(
        profile.judgment_counts.get("blunder", 0), profile.player_moves
    )
    phase_bits: list[str] = []
    for phase in ("opening", "middlegame", "endgame"):
        stats = profile.phases.get(phase)
        rate = (
            _rate(stats.judgment_counts.get("blunder", 0), stats.moves)
            if stats is not None
            else "n/a"
        )
        phase_bits.append(f"{phase} {rate}")
    return (
        f"- Quality: {_pawns_or_na(profile.overall_acpl)} ACPL overall, "
        f"{overall_blunder} blunders overall ({', '.join(phase_bits)})"
    )


def _profile_trend_line(profile: PlayerProfile) -> str | None:
    months = profile.months
    if not months:
        return None
    first, last = months[0], months[-1]
    first_rating = first.rating_end if first.rating_end is not None else "n/a"
    if len(months) == 1:
        return f"- Trend: {first.month} rating {first_rating}"
    last_rating = last.rating_end if last.rating_end is not None else "n/a"
    return (
        f"- Trend: {first.month} {first_rating} → {last.month} {last_rating} "
        f"({_plural(len(months), 'month')})"
    )


def _profile_opening_entry_with_color(o: ProfileOpening) -> str:
    color_word = "White" if o.color == "white" else "Black"
    return f"{color_word} {_profile_opening_entry(o)}"


def _profile_chosen_line(profile: PlayerProfile) -> str | None:
    chosen = [o for o in profile.openings if not o.faced]
    if not chosen:
        return None
    parts = "; ".join(_profile_opening_entry_with_color(o) for o in chosen)
    return f"- Chosen systems: {parts}"


def _profile_faced_line(profile: PlayerProfile) -> str | None:
    faced = [o for o in profile.openings if o.faced]
    if not faced:
        return None
    parts = "; ".join(_profile_opening_entry_with_color(o) for o in faced)
    return f"- Faced lines: {parts}"


def _profile_errors_line(profile: PlayerProfile) -> str | None:
    if not profile.error_patterns:
        return None
    parts = "; ".join(f"{e.label} ({e.count})" for e in profile.error_patterns)
    return f"- Errors: {parts}"


def render_profile_context(profile: PlayerProfile) -> str:
    """The ~250-token block `render_explain_prompt` and
    `render_game_chat_context` embed at the top when given a profile
    (docs/06-coach.md, "Player profile"): the facts one line each, then
    -- when `profile.narrative` is set -- the stored narrative under a
    "Coach's read" line. Total over `narrative=None`: renders the facts
    alone.
    """
    lines = ["## Student profile"]
    for line in (
        _profile_ratings_line(profile),
        _profile_quality_line(profile),
        _profile_trend_line(profile),
        _profile_chosen_line(profile),
        _profile_faced_line(profile),
        _profile_errors_line(profile),
    ):
        if line:
            lines.append(line)
    if profile.narrative is not None:
        lines.append("")
        lines.append("Coach's read:")
        lines.append(profile.narrative)
    return "\n".join(lines)


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


def render_explain_prompt(
    ctx: MoveContext,
    lines: list[EvalLine],
    *,
    profile: PlayerProfile | None = None,
) -> str:
    """Given a `profile`, the student-profile context block opens the
    prompt (docs/06-coach.md, "Player profile"); with `None` (the
    default) the prompt renders exactly as it always did -- an empty
    leading section is filtered out below like every other empty one.
    """
    sections = [
        render_profile_context(profile) if profile is not None else "",
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


# --- chat (docs/06-coach.md, "Chat") --------------------------------------
#
# Two scope seeds -- render_game_chat_context, render_report_chat_context --
# plus render_chat_prompt, the replay formatter every provider falls back
# to when it cannot resume a warm session. Both seeds close with the same
# engine-availability statement and the same chat instructions: the
# explain register rules (club player, idea before number, no redundant
# annotation) plus two chat-specific rules (claims from tools only, and
# app-relative game links minted only from tool-returned ids -- there is
# no append_game_links pass here, so no [gN] handle citation is offered).

_CHAT_INSTRUCTIONS = (
    "## How to respond\n"
    "- **Audience and register.** Write for a club player, not a fellow "
    "engine: pawns, never centipawns, and lead with the idea -- the "
    "threat, the plan, what a line wins -- before any number. Skip "
    'engine-style annotation -- no "?"/"??" next to a move you\'re also '
    "calling a mistake or blunder; say it once, in plain language.\n"
    "- **Claims from tools only.** Any claim about the student's games -- "
    "a result, a move, an opponent, a pattern -- must come from a tool "
    "result returned earlier in this conversation, never from memory of "
    "the context above or of an earlier turn. Look something up before "
    "asserting it, or say you don't know.\n"
    "- **Game links.** When you reference one of the student's games, "
    "link it with an app-relative markdown reference in the form "
    "`[text](/games/{id}?ply={n})`, using only a game id a tool result "
    "returned in this conversation -- never an id you have not seen from "
    "a tool result, and never a raw URL."
)


def _chat_engine_availability_line(engine_available: bool) -> str:
    if engine_available:
        return (
            "Engine analysis is available in this conversation: use the "
            "`analyze_position` tool to verify a concrete line before "
            "asserting it."
        )
    return (
        "Engine analysis is not available in this conversation: there is "
        "no `analyze_position` tool right now, so say a line is "
        "unverified rather than asserting it as fact."
    )


def _game_chat_identity_section(detail: GameDetail) -> str:
    date = _format_date(detail.end_time)
    color_word = "White" if detail.color == "white" else "Black"
    opening = f", {detail.opening.name}" if detail.opening else ""
    return (
        "## Game\n"
        f"{detail.username} played {color_word.lower()} against "
        f"{detail.opponent} on {date} ({detail.time_class}); result: "
        f"{detail.result}{opening}."
    )


def render_game_chat_context(
    detail: GameDetail,
    *,
    ply: int | None = None,
    lines: list[EvalLine] | None = None,
    engine_available: bool,
    profile: PlayerProfile | None = None,
) -> str:
    """The game-scope chat seed: the game's identity, plus -- when a ply
    anchor is set -- the same `MoveContext` fields and seeded eval lines
    `render_explain_prompt` uses. Raises `ValueError` when `ply` is out of
    range (`build_move_context` mirrors `render_explain_prompt`'s own
    check) or when the game has no analysis to anchor at all -- there is
    no `MoveContext` to build without one. Given a `profile`, the
    student-profile context block opens the seed exactly as in
    `render_explain_prompt`; with `None` (the default) the seed renders
    exactly as it always did.
    """
    sections: list[str] = []
    if profile is not None:
        sections.append(render_profile_context(profile))
    sections.append(_game_chat_identity_section(detail))
    if ply is not None:
        if detail.analysis is None:
            raise ValueError(
                f"game {detail.id} has no analysis; cannot anchor chat at ply {ply}"
            )
        ctx = build_move_context(detail, detail.analysis, detail.opening, ply)
        sections.append(_explain_positions(ctx))
        sections.append(_explain_move(ctx))
        sections.append(_explain_lines(lines or []))
    sections.append(_chat_engine_availability_line(engine_available))
    sections.append(_CHAT_INSTRUCTIONS)
    return "\n\n".join(section for section in sections if section)


def render_report_chat_context(report: PlayerReport, *, engine_available: bool) -> str:
    """The report-scope chat seed: the same data sections `render_prompt`
    shows, minus the coaching-brief instruction block -- the shared chat
    instructions replace it, since this is a conversation, not a request
    for a one-shot brief.
    """
    sections = [
        _student_section(report),
        _phase_section(report),
        _trend_section(report.months),
        _terminations_section(report),
        _repertoire_section(report),
        _error_patterns_section(report, {}),
        _turning_points_section(report),
        _chat_engine_availability_line(engine_available),
        _CHAT_INSTRUCTIONS,
    ]
    return "\n\n".join(section for section in sections if section)


def render_chat_prompt(history: list[ChatMessage], message: str) -> str:
    """The shared replay formatter every provider falls back to when it
    cannot resume a warm session (docs/06-coach.md, "Chat" -- "Replay"):
    prior turns as Student:/Coach: blocks, oldest first, then the new
    message -- so every provider replays an identical transcript.
    """
    blocks = [
        f"{'Student' if turn.role == 'user' else 'Coach'}: {turn.content}"
        for turn in history
    ]
    blocks.append(f"Student: {message}")
    return "\n\n".join(blocks)
