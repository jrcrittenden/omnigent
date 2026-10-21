"""File-backed review ledger shared by every policy evaluation path.

The server engine rebuilds policies per call and the runner-local gate has no
``session_state``, so durable state lives in one JSON file guarded by a lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_Json = dict[str, Any]


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def load(path: Path) -> _Json:
    """
    Read the ledger.

    :param path: Ledger file path.
    :returns: Ledger dict with ``dispatches`` (by title) and ``verdicts`` lists.
    """
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("dispatches", {})
    data.setdefault("verdicts", [])
    return data


def update(path: Path, mutate: Callable[[_Json], None]) -> None:
    """
    Apply *mutate* to the ledger under an exclusive lock, writing atomically.

    :param path: Ledger file path.
    :param mutate: In-place mutation of the loaded ledger dict.
    """
    with _locked(path):
        data = load(path)
        mutate(data)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name)
        with os.fdopen(fd, "w") as tmp_file:
            json.dump(data, tmp_file, indent=2, sort_keys=True)
        os.replace(tmp, path)
