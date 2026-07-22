"""Raw chess.com API response shapes (validated, extras ignored)."""

from pydantic import BaseModel, ConfigDict


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ArchiveIndex(_Raw):
    archives: list[str] = []


class MonthArchive(_Raw):
    games: list[dict[str, object]] = []


class RawPlayer(_Raw):
    username: str
    rating: int
    result: str


class RawAccuracies(_Raw):
    white: float | None = None
    black: float | None = None


class RawGame(_Raw):
    uuid: str
    pgn: str | None = None
    time_control: str = ""
    time_class: str = ""
    rules: str = ""
    end_time: int
    white: RawPlayer
    black: RawPlayer
    accuracies: RawAccuracies | None = None
