"""AnalysisRun pub/sub unit tests (the SSE fanout core)."""

from chess_coach.api.runs import AnalysisRun
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
