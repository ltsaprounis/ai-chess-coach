"""AnalysisRun pub/sub unit tests (the SSE fanout core), plus the
registry eviction sweep (docs/codebase-assessment-2026-07-30.md
finding 6)."""

from chess_coach.api.runs import AnalysisRun, evict_finished
from chess_coach.engine import Progress


def test_event_carries_current_counts_and_finished_flag() -> None:
    run = AnalysisRun(games_total=3)
    run.games_done = 2

    event = run.event("progress", Progress(game_id="g", ply=5, total_plies=40))
    assert (event.games_total, event.games_done, event.finished) == (3, 2, False)
    assert event.progress is not None and event.progress.ply == 5

    run.finished = True
    assert run.event("run_done").finished is True


async def test_publish_reaches_all_subscribers_until_unsubscribed() -> None:
    run = AnalysisRun(games_total=1)
    first = run.subscribe()
    second = run.subscribe()

    run.publish(run.event("game_done"))
    assert (await first.get()).type == "game_done"
    assert (await second.get()).type == "game_done"

    run.unsubscribe(first)
    run.publish(run.event("run_done"))
    assert first.empty()
    assert (await second.get()).type == "run_done"


def test_mark_finished_sets_finished_and_stamps_when() -> None:
    run = AnalysisRun(games_total=1)
    assert (run.finished, run.finished_at) == (False, None)

    run.mark_finished()
    assert run.finished is True
    assert run.finished_at is not None


def _finished_run(finished_at: float) -> AnalysisRun:
    run = AnalysisRun(games_total=1)
    run.finished = True
    run.finished_at = finished_at
    return run


def test_evict_finished_is_a_noop_under_the_cap() -> None:
    runs = {f"user{n}": _finished_run(float(n)) for n in range(3)}

    evict_finished(runs, keep=5)

    assert set(runs) == {"user0", "user1", "user2"}


def test_evict_finished_drops_the_oldest_finished_runs_first() -> None:
    # Inserted out of finished_at order, so a correct sweep has to sort
    # rather than rely on dict/insertion order.
    runs = {
        "user-mid": _finished_run(20.0),
        "user-newest": _finished_run(30.0),
        "user-oldest": _finished_run(10.0),
    }

    evict_finished(runs, keep=2)

    assert set(runs) == {"user-mid", "user-newest"}


def test_evict_finished_never_touches_a_run_whose_task_is_still_running() -> None:
    active = AnalysisRun(games_total=1)  # finished=False: task still going
    runs: dict[str, AnalysisRun] = {"active-user": active}
    for n in range(5):
        runs[f"finished-user-{n}"] = _finished_run(float(n))

    evict_finished(runs, keep=1)

    # The active run survives untouched, no matter how far its mere
    # presence pushes the registry over the cap.
    assert runs["active-user"] is active
    # Only the cap's worth of finished runs remain alongside it -- the
    # most recently finished one (highest finished_at).
    assert set(runs) == {"active-user", "finished-user-4"}


def test_evict_finished_leaves_an_all_active_registry_alone() -> None:
    runs = {f"user{n}": AnalysisRun(games_total=1) for n in range(3)}

    evict_finished(runs, keep=0)

    assert set(runs) == {"user0", "user1", "user2"}
