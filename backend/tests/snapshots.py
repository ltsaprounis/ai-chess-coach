"""Shared snapshot assertion for the prompt-template tests.

Prompts are user-visible artifacts, so their tests pin exact bytes
against files under tests/testdata/. Run pytest with
UPDATE_SNAPSHOTS=1 to rewrite the stored files instead of asserting
against them.
"""

import os
from pathlib import Path


def write_or_check(path: Path, text: str) -> None:
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(text)
    assert text == path.read_text()
