"""Cooperative stop of long jobs when the worker gets SIGTERM/SIGINT.

The worker's signal handler calls ``request_stop()``. The jobs pass ``commit_or_stop(session)``
as the scoring ``commit`` callback: after each scoring batch it commits (completed batches are
kept) and then raises ``WorkerStopping`` when a stop was requested. The job marks its run
``failed`` with ``STOPPING_MESSAGE`` and re-raises; the worker loop exits. So
``docker compose stop`` ends the worker within one batch instead of waiting for SIGKILL.
Work left over (pending bills, pairs without a score, profiles left ``running``) is picked up
after the restart, as after a crash (``worker.recover_interrupted``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from sqlalchemy.orm import Session

STOPPING_MESSAGE = "interrupted (worker stopping)"

_stop = threading.Event()


class WorkerStopping(Exception):  # noqa: N818 - a signal, not an error
    """A stop was requested; raised between scoring batches after committing them."""

    def __init__(self) -> None:
        super().__init__(STOPPING_MESSAGE)


def stop_event() -> threading.Event:
    return _stop


def request_stop() -> None:
    _stop.set()


def stop_requested() -> bool:
    return _stop.is_set()


def clear_stop() -> None:
    """Reset the flag (tests, or a new worker loop in the same process)."""
    _stop.clear()


def raise_if_stopping() -> None:
    if _stop.is_set():
        raise WorkerStopping()


def commit_or_stop(session: Session) -> Callable[[], None]:
    """Scoring ``commit`` callback: commit the batch, then stop if a stop was requested."""

    def _commit() -> None:
        session.commit()
        raise_if_stopping()

    return _commit
