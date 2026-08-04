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
    Color,
    Comparison,
    CriticalPosition,
    ErrorPattern,
    EvalLine,
    GameDetail,
    MonthStats,
    OpeningStats,
    PeriodStats,
    PlayerProfile,
    PlayerReport,
    ProfileOpening,
    Record,
    StreakStats,
    TerminationStats,
    TimeClassStats,
)

# Bumped whenever the template changes materially -- the API layer keys
# its report cache on this, so a reworded prompt invalidates cached advice
# instead of being served alongside a template that no longer exists.
# This bump: the template settled on one register (its data described the
# student in the third person in some sections and addressed them in the
# second in others, while its instructions -- which cannot use the second
# person for the student, since that is the model -- were third
# throughout), and the instructions now state the brief's own register
# instead of implying it through headings. Also SYSTEM_PROMPT, which the
# cache does not see: it stopped naming the coaching brief as the thing
# to write, so advice cached under the old persona would otherwise be
# served beside advice written under the new one. And the template
# stopped saying "ACPL": every loss figure now names pawns where the
# number is, so the glossary line that used to redefine the acronym as
# pawns is gone (docs/06-coach.md, "Units").
PROMPT_VERSION = "2026-07-one-register-one-unit"

# The narrative's own version, independent of PROMPT_VERSION above -- the
# report template and the profile prompt evolve on separate schedules
# (docs/06-coach.md, "Player profile"). Row metadata, never a cache key:
# a bump only flags the stored narrative as stale in the UI and must
# never trigger a silent re-bill.
# v2: the narrative became third-person (v1 addressed the student as
# "you", which reads as addressing the *coach* once the text is embedded
# in another prompt), gained its time-control scope, and gained the
# volume/quality coverage split plus recent-form windows.
# v3: the facts gained the volume-layer milestones (docs/06-coach.md,
# "Milestones") -- dated rating peaks, best win, streaks and the
# after-a-loss rebound, the color split, the opposition split, and how
# games end -- with instructions to use them.
# v4: two rules about the narrative's *durability*, both cases of the
# same thing -- it is written under this prompt and read under another.
# Spell units out, because a model knows "ACPL" whether or not a prompt
# defines it, and this text is read where nothing does; and no markdown
# headings, because it lands inside another prompt's sections. The
# embed also block-quotes the narrative, so the second is belt and
# braces. The templates stopped saying "ACPL" at the same time (see
# docs/06-coach.md, "Units"), so nothing here models the habit either.
PROFILE_PROMPT_VERSION = "profile-v10"

# Given to the LLM as its system prompt -- it replaces the Claude Code
# coding persona when running through the Agent SDK provider.
#
# `create_provider` builds one provider with one system prompt, and that
# provider serves all three non-chat artifacts: the report brief, the
# profile narrative and a move explanation. So this names none of them.
# A persona that says "respond with the coaching brief only" tells a
# model writing a move explanation to write a brief instead, and tells
# the narrative -- a briefing about the student, for another coach --
# that it is a brief for the student, contradicting its own instruction
# block. What all three share is the coach, the pre-computed figures and
# the do-what-the-instructions-say discipline; what they produce is the
# instruction block's business, and each one states it.
SYSTEM_PROMPT = (
    "You are a strong, practical chess coach working from a student's "
    "engine-analyzed games. Every figure you are given is already "
    "computed -- move-weighted, and carrying its own denominator where "
    "it has one -- so read the numbers as given rather than recomputing "
    "or re-averaging them. This is a coaching task, not a software "
    "task: write what the instruction block at the end asks for and "
    "nothing else, with no preamble about the nature of the request, "
    "and follow that block exactly."
)

# Given to the LLM as its system prompt for a chat turn (docs/06-coach.md,
# "Chat") -- SYSTEM_PROMPT above closes on an instruction block at the
# end of the prompt, which is not how a chat turn is shaped: the
# instructions arrive once in the seed and the turns that follow are a
# conversation, not a request for a finished piece. So chat gets its own
# persona line. The scope seed (render_game_chat_context /
# render_report_chat_context) is concatenated after this by the provider
# and carries the actual instructions (_CHAT_INSTRUCTIONS below).
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
    "- **Audience and register.** Write the brief *to* the student, in "
    'the second person ("you lose most of these on the clock"): the data '
    "above describes them in the third person, but they are the one "
    "reading what you write. Write for a club player, not a fellow "
    "engine: pawns, never centipawns, and lead with the idea -- the "
    "threat, the plan, what a line wins -- before any number.\n"
    "- **Attribution.** An opening is the student's own only where the "
    'repertoire lists it under their color in "Systems the student '
    'chose". Never advise dropping a line from the "What they face" '
    "table -- recommend a response to it instead.\n"
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
    "- **Recent form first.** Where the recent-form windows disagree "
    "with the whole-span figures, lead with the most recent window that "
    "has a real analyzed sample and say which way it is moving -- how "
    "they play now matters more than their average over years. Ignore a "
    "window whose analyzed count is too small to carry a conclusion, "
    "and prefer these windows to a single month's row, which swings on "
    "one bad game.\n"
    "- **Milestones are evidence.** The dated rating peak, the best "
    "win, the streaks, the score in the game right after a loss, the "
    "White/Black split and how games end are facts about every game in "
    "scope, not just the analyzed ones. Use the ones that say something "
    "-- sitting well below a peak reached long ago, a worse score right "
    "after a loss, a lopsided color split, a large share of losses on "
    "the clock each name a problem worth a paragraph -- and ignore the "
    "rest. Never read a split resting on a handful of games as a "
    "tendency: each carries its own game count, so check it first.\n"
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
        _periods_section(report.periods),
        _phase_section(report),
        _trend_section(report.months),
        _milestones_section(report),
        _terminations_section(report.terminations),
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
        "*(Losses are in pawns per move -- 0.35 means the average move "
        "gave up about a third of a pawn; lower is better. Every figure "
        "below is move-weighted across the games in scope.)*",
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
            f"(range {tc.rating_min}-{tc.rating_max}; peak {_peak_cell(tc)})"
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


# --- milestones (docs/06-coach.md, "Milestones") ------------------------
#
# Every data line here is subject-free ("Best win: beat marko77 ..."),
# like every other line of the student section it sits beside -- and
# like the rest of this template's data, which describes the student in
# the third person throughout. That is the register the instruction
# block was always in ("raise this student's results"), and the side
# that had to move was the data: second person inside an instruction
# addresses the *model*, not the student. The register of the
# brief the model writes is the opposite and unchanged -- second person,
# to the student -- and _INSTRUCTIONS now says so outright, where the
# old "Systems you chose" headings only implied it.
#
# `_after_loss_line` and `_color_split_line` are shared with the
# profile prompt outright -- subject-free leaves the two renderings
# identical, so there is one of each, taking the fields rather than
# either container.


def _report_best_win_line(report: PlayerReport) -> str | None:
    """Named by opponent and date, unlike the profile's version.

    The citation ban that strips the opponent from the profile's line
    exists because that text is stored and embedded where no game
    reference resolves. This brief is the opposite case: its whole
    citation rule is "name the game by opponent and date", and a
    milestone the student cannot go and find is not a milestone.
    """
    win = report.best_win
    if win is None:
        return None
    gap = win.opponent_rating - win.player_rating
    return (
        f"- Best win: beat {win.opponent} ({win.opponent_rating}) on "
        f"{_format_date(win.end_time)}, rated {win.player_rating} at the "
        f"time ({gap:+d})"
    )


def _report_streak_line(report: PlayerReport) -> str | None:
    """A run of one is worded as the last game's result -- see
    docs/06-coach.md, "Milestones"."""
    s = report.streaks
    if s is None:
        return None
    outcome = {"win": "a win", "loss": "a loss", "draw": "a draw"}[s.current_result]
    run = {"win": "winning", "loss": "losing", "draw": "drawn"}[s.current_result]
    current = (
        f"last game was {outcome}"
        if s.current_length == 1
        else f"on a {s.current_length}-game {run} run"
    )
    return (
        f"- Streaks: {current}; longest runs {s.longest_win} wins, "
        f"{s.longest_loss} losses"
    )


def _after_loss_line(streaks: StreakStats | None, record: Record) -> str | None:
    """Tilt, and only ever as a comparison -- the figure means nothing
    alone, since 39% is bad only against a better overall score.
    Omitted when no game follows a loss in the same sitting: that is a
    missing sample, and a 0% score would read as a catastrophic one
    (docs/06-coach.md, "Milestones").

    Shared verbatim by the report brief and the profile prompt: the
    line is subject-free, so neither register needs its own copy.
    """
    if streaks is None or streaks.after_loss.games == 0:
        return None
    return (
        f"- After a loss: {_score_line(streaks.after_loss)} in the next game "
        f"of the same sitting -- against {_score_line(record)} overall"
    )


def _color_split_line(color_records: dict[Color, Record]) -> str | None:
    """The overall score by side, which the repertoire tables cannot
    give: they are per family and past a sample floor, so a student who
    is simply worse with Black shows up nowhere in them. Shared by both
    prompts, like `_after_loss_line`.
    """
    if not color_records:
        return None
    parts = "; ".join(
        f"{color.capitalize()} {_score_line(record)}"
        for color, record in color_records.items()
        if record.games
    )
    return f"- By color: {parts}" if parts else None


def _milestones_section(report: PlayerReport) -> str:
    lines = [
        line
        for line in (
            _report_best_win_line(report),
            _report_streak_line(report),
            _after_loss_line(report.streaks, report.record),
            _color_split_line(report.color_records),
        )
        if line
    ]
    if not lines:
        return ""
    return "\n".join(
        [
            "## Milestones",
            "*(Over every game in scope, analyzed or not -- none of these "
            "needs an engine.)*",
            *lines,
        ]
    )


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
        # Which figures the shortfall actually touches. "Every figure
        # below describes only the analyzed span" was true when this
        # note was written and stopped being true with the volume/
        # quality split (docs/06-coach.md): ratings, records, the
        # repertoire's game counts, terminations, opposition and the
        # milestones all cover every stored game. Saying otherwise tells
        # the model to discount the half of the brief that is complete.
        lines.append(
            f"- Note: the other {_plural(missing, 'game')} in scope "
            f"{verb} not engine-analyzed. Ratings, records, milestones, "
            "how games end and the repertoire's game counts cover every "
            "game in scope; average loss, blunder rates, error patterns "
            "and "
            "turning points cover the analyzed ones only."
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
        "| Phase | Moves | Avg loss | Blunder % |",
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
        f"Overall {_pawns_or_na(report.overall_acpl)} pawns lost per move "
        f"over {total_moves} "
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
        "| Month | Games | Rating | Avg loss | Blunder % |",
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


def _terminations_section(terminations: list[TerminationStats]) -> str:
    """Renders `terminations` as-is -- shared by the report prompt and
    `render_profile_prompt`, which passes the profile's own copy of the
    same list (docs/06-coach.md, "Milestones"), exactly as
    `_trend_section` is shared for `months`.
    """
    if not terminations:
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
        rows = [t for t in terminations if t.result == result]
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
    lines = ["#### Systems the student chose"]
    if not families:
        lines.append(
            f"No line yet reaches the {REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
        )
        return "\n".join(lines)
    lines += [
        "| System (first moves) | Games | Score | Opening avg loss | Game avg loss |",
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
    lines = [f"#### What they face as {label}"]
    if not families:
        lines.append(
            f"No line yet reaches the {REPERTOIRE_SAMPLE_FLOOR}-game sample floor."
        )
        return "\n".join(lines)
    lines += [
        "| Opponent's line (their reply) | Games | Score "
        "| Opening avg loss | Game avg loss |",
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
        f"Played **{move_label}{p.played}** (lost {format_cp_loss(p.cp_loss)}): "
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


def _profile_games_phrase(profile: PlayerProfile) -> str:
    """ "rapid games" / "games (all time controls)" -- the scope every
    profile renderer names (docs/06-coach.md, "Player profile"). A
    profile covers one time control; saying which is what stops a rapid
    narrative from being read as the whole player. Bare, so each caller
    can attach whatever subject reads correctly where it sits.
    """
    if profile.time_class is None:
        return "games (all time controls)"
    return f"{profile.time_class} games"


def _profile_scope(profile: PlayerProfile) -> str:
    """The scope phrase with a pronoun -- "their rapid games". Only for
    `_profile_intro`, which sits under a header naming the student:
    `render_profile_context` opens on its header, where "their" would
    have no antecedent at all, so it names the student instead.
    """
    return f"their {_profile_games_phrase(profile)}"


def _profile_coverage(profile: PlayerProfile) -> str:
    """The two denominators, stated (docs/06-coach.md, "Volume and
    quality"): how many games the volume figures describe and how many
    of them the engine has actually analyzed. Without this the model
    reads every figure against one number and treats a quarter-analyzed
    archive's average loss as the player's settled quality.
    """
    if profile.games_in_scope <= profile.games_covered:
        return f"all {_plural(profile.games_covered, 'game')} analyzed"
    # The analyzed subset's own *span*, not just its size. Stating a
    # bare count let the first live narrative call a seven-month quality
    # figure the student's "whole span", because nothing said the
    # analyzed games were all recent (docs/06-coach.md, "Coverage").
    span = ""
    if (
        profile.analyzed_window_start is not None
        and profile.analyzed_window_end is not None
    ):
        span = (
            f", all from {_format_date(profile.analyzed_window_start)} to "
            f"{_format_date(profile.analyzed_window_end)}"
        )
    return (
        f"{profile.games_covered} of {profile.games_in_scope} analyzed{span} -- "
        "ratings, records and repertoire counts cover every game; "
        "average loss, blunder rates and error patterns cover the "
        "analyzed ones"
    )


def _profile_intro(profile: PlayerProfile) -> str:
    window = ""
    if profile.window_start is not None and profile.window_end is not None:
        window = (
            f", {_format_date(profile.window_start)} to "
            f"{_format_date(profile.window_end)}"
        )
    # The window holds the student's level roughly constant, so every
    # figure below describes one player. When it could not -- too thin a
    # sample at this level -- that is said outright rather than left for
    # a reader to infer from dates (docs/06-coach.md, "Window").
    caveat = (
        "\n*(This span covers a change in the student's level: there "
        "were too few games at their current one to describe it alone, "
        "so the rates below average across more than one player.)*"
        if profile.window_spans_level_change
        else ""
    )
    return (
        f"# Player profile -- {profile.username}\n"
        "*(Losses are in pawns per move -- 0.35 means the average move "
        "gave up about a third of a pawn; lower is better. Every figure "
        "below is move-weighted over the games covered.)*\n"
        f"Covering {_profile_scope(profile)} at their current level: "
        f"{_plural(profile.games_in_scope, 'game')}{window} "
        f"({_profile_coverage(profile)}).{caveat}"
    )


def _periods_section(periods: list[PeriodStats]) -> str:
    """Recent form as trailing windows (docs/06-coach.md, "Recent
    form"), so a reader can weight how the student plays now over how
    they played a year ago. Empty when the builder produced no windows
    -- a span too short to slice.

    Shared by `render_prompt` and `render_profile_prompt` over their own
    `periods` lists, exactly like `_trend_section` is for `months`: the
    table is register-free, so unlike the repertoire and milestone
    sections it needs no second-person variant.
    """
    if not periods:
        return ""
    lines = [
        "## Recent form",
        "*(Windows are nested and end at the most recent game, so each "
        "wider row contains the narrower ones.)*",
        "| Window | Games | Score | Rating | Avg loss | Blunder % | Analyzed |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in periods:
        rating = p.rating_end if p.rating_end is not None else "n/a"
        blunder = (
            f"{p.blunder_rate * 100:.1f}%" if p.blunder_rate is not None else "n/a"
        )
        lines.append(
            f"| {p.label} | {p.games} | {_score_line(p.record)} | {rating} "
            f"| {_pawns_or_na(p.acpl)} | {blunder} | {p.analyzed_games} |"
        )
    return "\n".join(lines)


def _profile_ratings_section(profile: PlayerProfile) -> str:
    """Rating movement per control, with the peak dated (docs/06-coach.md,
    "Milestones").

    The peak gets its own column rather than staying folded into the
    range, because a peak and a range answer different questions: how
    far the student has ever been is only coaching signal next to *when*
    they were there and how far below it they sit now.
    """
    if not profile.time_classes:
        return ""
    # The gap is only a milestone for a student who is not improving,
    # and the trajectory section is the only thing that knows which.
    show_gap = profile.trajectory is None or not profile.trajectory.improving
    lines = [
        "## Ratings",
        "| Time class | Score | Rating | Peak |",
        "|---|---|---|---|",
    ]
    for tc in profile.time_classes:
        lines.append(
            f"| {tc.time_class.capitalize()} | {_score_line(tc.record)} "
            f"| {tc.rating_start} → {tc.rating_end} "
            f"(range {tc.rating_min}-{tc.rating_max}) "
            f"| {_peak_cell(tc, show_gap=show_gap)} |"
        )
    return "\n".join(lines)


def _peak_cell(tc: TimeClassStats, *, show_gap: bool = True) -> str:
    """ "1496 on 2026-06-10, -42 since" -- the date is what makes a peak
    a milestone, and the gap is what makes it actionable. Renders
    without the date on a profile snapshot stored before the field
    existed (`rating_max_at` is None only there).

    `show_gap=False` drops the trailing gap for a student the trajectory
    says is improving (docs/06-coach.md, "Trajectory"). Without it this
    cell contradicts the trajectory section three lines above it: one
    saying "up 443 points over the year", the other "-95 since", about
    the same student in the same document.
    """
    peak = str(tc.rating_max)
    if tc.rating_max_at is not None:
        peak += f" on {_format_date(tc.rating_max_at)}"
    if show_gap and tc.rating_end < tc.rating_max:
        peak += f", {tc.rating_end - tc.rating_max} since"
    return peak


def _opposition_line(profile: PlayerProfile) -> str | None:
    """The same opposition split the report's student section renders,
    over the profile's own copy -- a student who only beats weaker
    players is a different coaching problem from one who holds their
    own above their rating.
    """
    o = profile.opponents
    if o is None:
        return None
    return (
        f"- Opposition: avg rating diff {o.avg_rating_diff:+.0f}; "
        f"{_score_line(o.vs_stronger)} vs stronger, "
        f"{_score_line(o.vs_similar)} vs similar, "
        f"{_score_line(o.vs_weaker)} vs weaker"
    )


def _best_win_line(profile: PlayerProfile) -> str | None:
    """The one line in this prompt naming a single game -- and it names
    no opponent (docs/06-coach.md, "Narrative"): the citation ban exists
    because this text is embedded where a game reference cannot be
    resolved, and an opponent's handle is exactly such a reference. The
    rating and the date are what make the milestone legible.
    """
    win = profile.best_win
    if win is None:
        return None
    gap = win.opponent_rating - win.player_rating
    # Gap first, because the gap is the achievement. "Beat a 1559" only
    # says the student was once rated about 1559 themselves -- chess.com
    # pairs by rating, so the highest-rated opponent beaten is
    # structurally their own peak (docs/06-coach.md, "Trajectory").
    return (
        f"- Biggest upset: beat someone {gap} points higher, a "
        f"{win.opponent_rating} on {_format_date(win.end_time)} while "
        f"rated {win.player_rating}"
    )


def _streak_line(profile: PlayerProfile) -> str | None:
    """A run of one is not a run, so it is worded as what it is -- the
    last game's result -- rather than as "a 1-game winning run", which
    invites a model to narrate momentum that does not exist.
    """
    s = profile.streaks
    if s is None:
        return None
    outcome = {"win": "a win", "loss": "a loss", "draw": "a draw"}[s.current_result]
    run = {"win": "winning", "loss": "losing", "draw": "drawn"}[s.current_result]
    current = (
        f"their last game was {outcome}"
        if s.current_length == 1
        else f"on a {s.current_length}-game {run} run"
    )
    return (
        f"- Streaks: {current}; longest runs {s.longest_win} wins, "
        f"{s.longest_loss} losses"
    )


def _profile_milestones_section(profile: PlayerProfile) -> str:
    """The volume layer's own findings (docs/06-coach.md, "Milestones"):
    what the student has managed, how they are running, and which side
    of the board they are worse on. None of it needs an engine, so all
    of it covers every game in scope rather than the analyzed subset --
    the section states that once, so the model does not read these
    against the analyzed count in the header.
    """
    # The after-a-loss and color-split lines are deliberately absent
    # here, though the report brief still renders them: this document
    # has a "Splits" section that states the same two comparisons *with
    # a verdict*. Rendering both would put the raw gap above the
    # judgement of it, and a model handed "48% after a loss against 52%
    # overall" as a milestone will narrate it whatever the section below
    # says (docs/06-coach.md, "Reading a comparison").
    lines = [
        line
        for line in (
            _best_win_line(profile),
            _streak_line(profile),
            _opposition_line(profile),
        )
        if line
    ]
    if not lines:
        return ""
    return "\n".join(
        [
            "## Milestones and tendencies",
            "*(All over every game in scope, analyzed or not -- none of "
            "these needs an engine.)*",
            *lines,
        ]
    )


def _profile_quality_section(profile: PlayerProfile) -> str:
    """Mirrors `_phase_section`'s shape over `PlayerProfile`'s own field
    names (`games_covered` where the report has `games_analyzed`) -- kept
    as its own function rather than a shared parametrization, since the
    two source types diverge in exactly that one field.
    """
    lines = [
        "## Quality",
        "| Phase | Moves | Avg loss | Blunder % |",
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
        f"Overall {_pawns_or_na(profile.overall_acpl)} pawns lost per move "
        f"over {total_moves} "
        f"moves -- {quality}; {blunders_per_game} blunders/game."
    )
    return "\n".join(lines)


def _profile_trajectory_section(profile: PlayerProfile) -> str:
    """Where the student is heading, over the **full** archive
    (docs/06-coach.md, "Trajectory") -- stated as covering more than the
    window above it, since every other figure in this document is
    level-scoped and a reader has no other way to tell.
    """
    t = profile.trajectory
    if t is None:
        return ""
    lines = [
        "## Trajectory",
        "*(The whole archive in this time control, not the window above "
        "-- direction is the one thing a level-scoped window cannot "
        "show.)*",
        f"- Now {t.rating_now} over {_plural(t.games, 'game')}",
    ]
    if t.deltas:
        moves = "; ".join(
            f"{d.delta:+d} over {d.days} days ({_plural(d.games, 'game')})"
            for d in t.deltas
        )
        lines.append(f"- Change: {moves}")
    # The peak gap is a headline only for a student who is not
    # improving: "95 below peak" on someone up 443 on the year is a
    # misread, and it is the one the first live narrative made.
    peak = f"- Peak {t.rating_max} on {_format_date(t.rating_max_at)}"
    if not t.improving and t.rating_now < t.rating_max:
        peak += f", {t.rating_now - t.rating_max} since"
    lines.append(peak)
    lines.append(f"- Low {t.rating_min} on {_format_date(t.rating_min_at)}")

    d = t.drawdown
    if d is not None:
        recovery = (
            f"recovered since ({_score_line(d.since_record)})"
            if d.recovered
            else f"since then {_score_line(d.since_record)}"
        )
        lines.append(
            f"- Largest setback: {d.depth} points, {d.peak} on "
            f"{_format_date(d.peak_at)} to {d.trough} on "
            f"{_format_date(d.trough_at)} -- {_score_line(d.record)} "
            f"through the fall; {recovery}"
        )
    return "\n".join(lines)


def _comparison_line(c: Comparison) -> str:
    """One matched comparison with its verdict (docs/06-coach.md,
    "Reading a comparison").

    The verdict is stated; the arithmetic behind it is not. Sigmas and
    p-values are not this audience's vocabulary, and a number the reader
    cannot calibrate invites exactly the false confidence the guard
    exists to remove.
    """
    body = (
        f"- {c.label}: {_score_line(c.left)} {c.left_label}, against "
        f"{_score_line(c.right)} {c.right_label}"
    )
    # A comparison with a baseline is not tested against zero, and a
    # reader who assumes it is will misread both verdicts: "within
    # noise" would look like "no difference" where it means "no more
    # than everyone has", and "a real difference" like any gap at all.
    if c.baseline:
        body += (
            f" (a {c.baseline:.0f}-point edge is normal for everyone and "
            "is already allowed for)"
        )
    if c.significant:
        return f"{body} -- a real difference"
    return f"{body} -- within noise, not a tendency"


def _profile_comparisons_section(profile: PlayerProfile) -> str:
    """Only the splits that could actually be measured.

    An unmeasurable one has nothing to say in either direction, and
    rendering it as "n/a ... too few games to compare" spends lines on
    the absence of a finding -- in a document whose whole problem was
    that the repertoire kept losing the budget to milestones.
    """
    rows = [c for c in profile.comparisons if c.measurable]
    if not rows:
        return ""
    return "\n".join(
        [
            "## Splits",
            "*(Each compares two groups of the student's own games. A "
            'split marked "within noise" is a difference this many '
            "games cannot distinguish from chance -- it is not a "
            "tendency and must not be reported as one.)*",
            *(_comparison_line(c) for c in rows),
        ]
    )


def _profile_opening_entry(o: ProfileOpening) -> str:
    """Games, score, and -- when the engine has reached the family --
    what the opening itself costs.

    The loss column is what makes a repertoire row coaching signal
    rather than a scoreboard: a system the student scores 48% in while
    leaking 0.32 pawns a move out of the book is a different problem
    from one they score 48% in at 0.21, and score alone cannot tell
    them apart.
    """
    entry = f"{o.name} ({o.moves}) -- {o.games}g, {o.score * 100:.0f}%"
    if o.opening_acpl is not None:
        entry += f", {_pawns_or_na(o.opening_acpl)} pawns/move out of the opening"
    return entry


def _profile_repertoire_section(profile: PlayerProfile) -> str:
    if not profile.openings:
        return ""
    parts = ["## Repertoire"]
    colors: tuple[tuple[Color, str], ...] = (("white", "White"), ("black", "Black"))
    for color, label in colors:
        rows = [o for o in profile.openings if o.color == color]
        if not rows:
            continue
        total = profile.color_records.get(color)
        parts.append(
            _profile_repertoire_color_section(
                label, rows, total.games if total is not None else None
            )
        )
    return "\n\n".join(parts)


def _profile_repertoire_color_section(
    label: str, rows: list[ProfileOpening], color_games: int | None
) -> str:
    """Third person throughout, like every other line of this prompt --
    the narrative it produces is stored and pasted into other prompts,
    where "you" addresses the coach reading it, not the student
    (docs/06-coach.md, "Narrative").

    The heading states how many games these rows actually cover. Without
    it a reader sees three families totalling 640 games and cannot tell
    whether that is the whole of a 640-game repertoire or two thirds of
    a 968-game one -- so "they open 1.d4 in essentially every game", the
    single most useful sentence about this student, is unsayable.
    """
    covered = sum(o.games for o in rows)
    header = f"### As {label}"
    if color_games:
        header += f" ({covered} of {_plural(color_games, 'game')} in these lines)"
    chosen = [o for o in rows if not o.faced]
    faced = [o for o in rows if o.faced]
    lines = [header]
    if chosen:
        lines.append("Systems the student chose:")
        lines.extend(f"- {_profile_opening_entry(o)}" for o in chosen)
    if faced:
        lines.append(f"What they face as {label}:")
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


# The whole instruction block (docs/06-coach.md, "The instructions say
# what the text is for"). It ran to twelve bullets of shape and never
# once said what the narrative was *for*, so the model optimized the
# only thing it had been given and the repertoire -- which no bullet
# named -- lost the sentence budget in every run. Twelve rules could not
# make it mention the openings; one sentence of purpose does, because a
# text written to be useful in another session has to say what the
# student plays.
#
# What survives is only what nothing else can supply. Everything cut was
# either a shape constraint the purpose statement implies, or an
# instruction to say something the facts already say -- recency being
# the clearest case, since it is now the window rather than a bullet.
_PROFILE_INSTRUCTIONS = (
    "## Instructions\n"
    "Write a short profile of this student for the coach who works with "
    "them next. It gets pasted into other sessions as context when that "
    "coach explains a move or answers a question, so write what would "
    "actually change the advice.\n\n"
    "**Dense, not polished. Around 200 words.** This is context another "
    "prompt pastes in, not an essay -- it is read for what it says, "
    "never for how it reads. Every sentence must carry a fact or a "
    "consequence the coach would act on. Cut transitions, cut any "
    "sentence whose only job is to introduce the next one, and never "
    "explain the significance of a figure you have just given: the "
    "reader is a coach and can see it. If a sentence could be deleted "
    "without losing information, delete it.\n\n"
    "{facts_clause}\n\n"
    "Seven rules, all because this text is stored and reused elsewhere:\n"
    "- Third person, about the student, to a coach. Never address the "
    'reader as "you" -- they are the coach, not the player.\n'
    "- No game citations, dates, opponents, links or handles. They "
    "resolve to nothing where this lands.\n"
    "- No markdown headings (`#`, `##`). This text is pasted *inside* "
    "another prompt's sections, where a heading of your own reads as "
    "starting a new one.\n"
    "- A claim about the position itself -- a structure, a plan, why a "
    "line is awkward -- is either verified or not made. The facts are "
    "aggregates; the move sequences printed beside them are the only "
    "concrete lines you have, and they are frequently the "
    "counterexample to a guess.\n"
    "- Every figure covers the same games as the facts above, including "
    "anything a tool returns. State a count only when you have it from "
    "the facts or a tool -- never rolled up in your head from rows.\n"
    "- An observation from reading individual games is an example, "
    "never a tendency. Say how many games you looked at, and do not "
    "turn it into a trait -- collapsing in three sampled endings is "
    "three endings, not a temperament.\n"
    '- Spell every unit out -- "1.30 pawns a move", never "1.30 ACPL" '
    "or any other acronym. Nothing here defines one, and neither do the "
    "prompts this lands in.\n\n"
    'A comparison marked "within noise" is not a tendency. Do not name '
    'it as a weakness, do not call it "worth watching", and do not '
    "soften it into a passing mention -- the honest statement is that "
    "the data cannot tell, and the sentence is better spent on "
    "something it can."
)

# The one clause that depends on how the run is actually executed. A
# prompt that says "use the tools" to a run with no tools is worse than
# one that never mentions them: the model either invents the lookups it
# was told to do, or spends its turn saying it cannot. Same conditional
# shape as `_explain_instructions`, and for the same reason.
_PROFILE_FACTS_ONLY = (
    "The facts above are everything you have -- there are no tools on "
    "this run, so write only what they support and say so where they "
    "run out."
)
_PROFILE_FACTS_WITH_TOOLS = (
    "The facts above are computed and correct -- your starting point, "
    "not your limit. Use the tools to check anything the summary rests "
    "on, and to find what the aggregates cannot show."
)


def render_profile_prompt(profile: PlayerProfile, *, has_tools: bool = False) -> str:
    """The narrative-generation prompt (docs/06-coach.md, "Player
    profile"): the facts -- fuller than `render_profile_context`, e.g. a
    full months table rather than one compact trend line -- followed by
    instructions stating what the text is *for* and the four rules
    nothing else can supply.

    `has_tools` says whether this run can actually look anything up. It
    changes exactly one clause: telling a tool-less run to "use the
    tools" makes the model either invent the lookups it was told to do
    or spend its turn explaining that it cannot.
    """
    facts_clause = _PROFILE_FACTS_WITH_TOOLS if has_tools else _PROFILE_FACTS_ONLY
    sections = [
        _profile_intro(profile),
        _profile_trajectory_section(profile),
        _profile_ratings_section(profile),
        _periods_section(profile.periods),
        _profile_quality_section(profile),
        _trend_section(profile.months),
        _profile_milestones_section(profile),
        _profile_comparisons_section(profile),
        _terminations_section(profile.terminations),
        _profile_repertoire_section(profile),
        _profile_error_patterns_section(profile),
        _PROFILE_INSTRUCTIONS.format(facts_clause=facts_clause),
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
    """Spells the unit where the number is -- "1.07 pawns lost per move",
    never "1.07 ACPL". `_pawns_or_na` divides by 100, so the acronym's
    own expansion (average *centipawn* loss) contradicts the figure by a
    factor of a hundred. `_profile_intro` gets away with it because it
    opens on a glossary line; the embedded block has no budget for one
    and lands inside hosts that say "pawns, never centipawns" outright.
    """
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
        f"- Quality: {_pawns_or_na(profile.overall_acpl)} pawns lost per move, "
        f"{overall_blunder} blunders overall ({', '.join(phase_bits)})"
    )


def _profile_trajectory_line(profile: PlayerProfile) -> str | None:
    """Direction, in one line, for the embedded block (docs/06-coach.md,
    "Trajectory").

    The longest measured span rather than all four: what a host prompt
    needs settled is "is this student climbing, stuck, or falling", and
    one number answers it. A live drawdown is appended when it has not
    been recovered, because a student still below a recent peak is a
    different person to explain a move to.
    """
    t = profile.trajectory
    if t is None or not t.deltas:
        return None
    longest = t.deltas[-1]
    line = (
        f"- Trajectory: {t.rating_now} now, {longest.delta:+d} over the "
        f"last {longest.days} days"
    )
    d = t.drawdown
    if d is not None and not d.recovered and d.depth <= -100:
        line += f"; still below a {d.peak} peak ({d.depth} at the worst)"
    return line


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


def _profile_coverage_line(profile: PlayerProfile) -> str | None:
    """The embedded block's own coverage statement -- omitted when the
    whole scope is analyzed, since "N of N" is noise in a block whose
    entire budget is ~250 tokens.
    """
    if profile.games_in_scope <= profile.games_covered:
        return None
    return (
        f"- Coverage: {profile.games_covered} of {profile.games_in_scope} games "
        "analyzed (quality figures below cover the analyzed ones; "
        "ratings, records and repertoire cover all)"
    )


def _profile_recent_line(profile: PlayerProfile) -> str | None:
    """The narrowest recent window with an analyzed sample, next to the
    all-time figure it should be read against (docs/06-coach.md, "Recent
    form"). One line, not the whole table: the block embeds into other
    prompts under a tight token budget, and the question those prompts
    need answered is "is this student better or worse than their average
    right now", which two numbers settle.
    """
    recent = next(
        (p for p in profile.periods if p.days is not None and p.analyzed_games > 0),
        None,
    )
    if recent is None or recent.acpl is None:
        return None
    blunder = (
        f", {recent.blunder_rate * 100:.1f}% blunders"
        if recent.blunder_rate is not None
        else ""
    )
    return (
        f"- Recent form ({recent.label}): {_pawns_or_na(recent.acpl)} pawns "
        f"lost per move{blunder} over "
        f"{_plural(recent.analyzed_games, 'analyzed game')} "
        f"-- against {_pawns_or_na(profile.overall_acpl)} pawns over the "
        "whole span"
    )


# How many termination codes the embedded block names. The block's whole
# budget is ~250 tokens and the shares are stated against the real loss
# total, so a truncated tail costs nothing the reader can be misled by.
_CONTEXT_TERMINATIONS = 3


def _profile_losing_line(profile: PlayerProfile) -> str | None:
    """How the student loses -- the one milestone that changes advice
    about a *single move*, which is what this block is embedded into.
    "You were winning here and lost on time" is a different lesson from
    "you were winning here and blundered", and only this line tells the
    reading coach which one is in character.

    Omitted when a single code covers every loss: it would restate the
    record above it, the same reason `_terminations_section` collapses a
    one-code result.
    """
    rows = [t for t in profile.terminations if t.result == "loss"]
    if len(rows) < 2:
        return None
    total = sum(t.games for t in rows)
    detail = ", ".join(
        f"{t.termination} {t.games / total * 100:.0f}%"
        for t in rows[:_CONTEXT_TERMINATIONS]
    )
    # Not `_plural`, whose rule is a bare "s" ("losss").
    noun = "loss" if total == 1 else "losses"
    return f"- How they lose: {total} {noun} -- {detail}"


def _blockquote(text: str) -> str:
    """Quote every line, blanks included, so the passage is one quote
    rather than two with a gap. Used on model-written text pasted into a
    prompt: the marker makes the extent unambiguous whatever the text
    turns out to contain.
    """
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def render_profile_context(profile: PlayerProfile) -> str:
    """The ~250-token block `render_explain_prompt` and
    `render_game_chat_context` embed at the top when given a profile
    (docs/06-coach.md, "Player profile"): a header naming the student
    and the time control, the facts one line each, then -- when
    `profile.narrative` is set -- the stored narrative, block-quoted,
    under a "Coach's read" line. Total over `narrative=None`: renders
    the facts alone.

    The header names the student because this is the *first* line of
    every prompt that embeds the block, where `_profile_scope`'s "their"
    would refer to nobody: the block never names them otherwise, and the
    host's own username line comes further down.

    The narrative is quoted because nothing constrains its structure --
    `_PROFILE_INSTRUCTIONS` asks for sentences plus bullets and never
    forbids a heading, and a narrative opening "## Tendencies" would
    otherwise forge a section boundary in the host prompt, handing every
    section after it to the narrative.
    """
    lines = [
        f"## Student profile -- {profile.username}, {_profile_games_phrase(profile)}"
    ]
    for line in (
        _profile_coverage_line(profile),
        _profile_ratings_line(profile),
        _profile_trajectory_line(profile),
        _profile_quality_line(profile),
        _profile_recent_line(profile),
        _profile_trend_line(profile),
        _profile_losing_line(profile),
        _profile_chosen_line(profile),
        _profile_faced_line(profile),
        _profile_errors_line(profile),
    ):
        if line:
            lines.append(line)
    if profile.narrative is not None:
        lines.append("")
        lines.append("Coach's read:")
        lines.append(_blockquote(profile.narrative))
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

# Appended only when the prompt opens with the profile block, so the
# profile-less prompt stays byte-identical (docs/06-coach.md,
# "Embedding") and the instruction never points at a section that is not
# there. One clause and no more: the block is context, not the subject,
# and these instructions are the one part of this prompt on a strict
# length budget.
_EXPLAIN_PROFILE_CLAUSE = (
    "The student profile above describes this same student -- pitch the "
    "explanation at that player, and where this move is an instance of a "
    "pattern the profile already counts, say so."
)


def _explain_instructions(*, has_profile: bool) -> str:
    if not has_profile:
        return _EXPLAIN_INSTRUCTIONS
    return f"{_EXPLAIN_INSTRUCTIONS} {_EXPLAIN_PROFILE_CLAUSE}"


def render_explain_prompt(
    ctx: MoveContext,
    lines: list[EvalLine],
    *,
    profile: PlayerProfile | None = None,
) -> str:
    """Given a `profile`, the student-profile context block opens the
    prompt and the instructions gain the clause that tells the model to
    use it (docs/06-coach.md, "Player profile") -- unexplained context
    is context a model may ignore, and the block's whole payoff is an
    explanation pitched at this student. With `None` (the default) the
    prompt renders exactly as it always did -- an empty leading section
    is filtered out below like every other empty one.
    """
    sections = [
        render_profile_context(profile) if profile is not None else "",
        _explain_intro(ctx),
        _explain_positions(ctx),
        _explain_move(ctx),
        _explain_lines(lines),
        _explain_instructions(has_profile=profile is not None),
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
# annotation) plus six chat-specific rules -- e.g. stated facts are
# usable and everything past them needs a tool result, and app-relative
# game links minted only from tool-returned ids (there is no
# append_game_links pass here, so no [gN] handle citation is offered).
#
# The first of those is scoped to what the seed does *not* state on
# purpose. Banning the seed itself -- which the rule did until the
# distinction was drawn -- bans the report scope's whole ~1,650-token
# briefing, and the game scope's own result and played move, before the
# first message: no tool has run yet, so the model could assert nothing
# at all about the student it had just been briefed on.

_CHAT_INSTRUCTIONS = (
    "## How to respond\n"
    "- **Audience and register.** Write for a club player, not a fellow "
    "engine: pawns, never centipawns, and lead with the idea -- the "
    "threat, the plan, what a line wins -- before any number. Skip "
    'engine-style annotation -- no "?"/"??" next to a move you\'re also '
    "calling a mistake or blunder; say it once, in plain language.\n"
    "- **Stated facts, or a tool result.** The facts stated in the "
    "context above are established: use them and quote them freely. "
    "Anything past them -- another game, another result, an opponent or "
    "a move not shown here -- must come from a tool result returned in "
    "this conversation. Never fill the gap from memory: look it up "
    "first, or say you don't know.\n"
    "- **Game links.** When you reference one of the student's games, "
    "link it with an app-relative markdown reference in the form "
    "`[text](/games/{id}?ply={n})`, using only a game id a tool result "
    "returned in this conversation -- never an id you have not seen from "
    "a tool result, and never a raw URL.\n"
    "- **Coverage honesty.** When you answer from a find_games or "
    "scan_games result, state its own totals and denominators -- how "
    "many matched, how many were scanned, how many had no analysis -- "
    "and offer to widen the search rather than presenting a partial "
    "look as the whole picture. Matches are EXAMPLES to read, never a "
    "tendency: only compare_groups establishes one. When a scan_games "
    "result is truncated and the question spans the student's whole "
    "history, continue the sweep from the result's own resume cursor -- "
    "repeat scan_games with until set to the stated resume value -- "
    "before concluding, rather than answering from the partial sweep.\n"
    "- **Featuring a sacrifice.** Before presenting a specific "
    "scan_games sacrifice hit as an example -- not when you are merely "
    "counting or listing matches -- call get_game with its id and ply "
    "and read the surrounding moves. A hit's ply is where the piece "
    "first sits capturable, not always where it was offered: when the "
    "flagged move answered a check, or the piece fell only through a "
    "forcing sequence, attribute the sacrifice to the last "
    "freely-chosen move before that sequence and confirm it with "
    "analyze_position on the position before that move.\n"
    "- **Dates.** Game times are stored as UTC epoch seconds. When the "
    'student names a calendar day ("the game on March 7th"), widen the '
    "search by a day on each side before concluding nothing matches -- "
    "a late-evening game in their own timezone can land on the next UTC "
    'day, and "no such game" for one they vividly remember is the worst '
    "answer available.\n"
    "- **Event fit.** When no scan_games event or chain matches what "
    'the student is asking ("games where I slowly strangled a knight"), '
    "say so plainly and fall back to metadata search plus reading "
    "rather than stretching the nearest event to cover it."
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
        _periods_section(report.periods),
        _phase_section(report),
        _trend_section(report.months),
        _milestones_section(report),
        _terminations_section(report.terminations),
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
