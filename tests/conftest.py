from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest


def _point_home_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Redirect home resolution to `path` on every platform.

    `Path.home()` reads `USERPROFILE`, then `HOMEDRIVE` + `HOMEPATH`, on Windows
    and only falls back to `HOME`. Setting `HOME` alone therefore leaves Windows
    runs resolving to the real profile, so a test that exercises the default
    discovery path writes generated config straight into it.
    """
    drive, tail = os.path.splitdrive(str(path))
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail)


@pytest.fixture(autouse=True)
def _sandbox_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Contain a forgotten or mis-set home inside the test's own `tmp_path`.

    Applies to every test so that no future case can reach the real profile,
    including one that never thinks about the home directory at all.
    """
    _point_home_at(monkeypatch, tmp_path / "_sandbox_home")


@pytest.fixture
def set_home(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], None]:
    """Point home resolution at a chosen path for tests that assert on it."""

    def _set(path: Path) -> None:
        _point_home_at(monkeypatch, path)

    return _set
