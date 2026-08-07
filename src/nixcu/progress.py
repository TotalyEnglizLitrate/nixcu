"""Loading progress for the phases that run before the TUI takes over.

Querying a system closure and building its dominator tree takes a few seconds
of dead air. The work is a fixed sequence of coarse phases, so the bar is
scaled in phases: each one fills a single slot. A phase that knows how much
work it has (reading store paths knows the entry count) declares a ``total``
and fills its slot incrementally; the opaque ones (``nix path-info``, the
graph passes) just sit on the spinner until they finish.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, final, override

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

__all__ = ["Reporter", "loading", "silent"]


class Reporter(Protocol):
    """Sink for loading progress."""

    def phase(self, name: str, total: int | None = None) -> None:
        """Announce that ``name`` is starting, over ``total`` steps if known."""

    def advance(self, amount: int = 1) -> None:
        """Record ``amount`` steps of the current phase. No-op if it had no total."""


@final
class _Silent(Reporter):
    """Reporter that draws nothing, for library callers and tests."""

    @override
    def phase(self, name: str, total: int | None = None) -> None:
        pass

    @override
    def advance(self, amount: int = 1) -> None:
        pass


silent: Reporter = _Silent()


@final
class _BarReporter(Reporter):
    """Maps phases onto one rich task whose total is the phase count."""

    def __init__(self, progress: Progress, task: TaskID, phases: int) -> None:
        self._progress = progress
        self._task = task
        self._phases = phases
        self._started = 0
        self._steps_total: int | None = None
        self._steps = 0

    @override
    def phase(self, name: str, total: int | None = None) -> None:
        # Everything before this phase is finished, so the bar snaps to a
        # whole slot even if the previous phase under-reported its steps.
        self._progress.update(self._task, description=name, completed=self._started)
        self._started += 1
        self._steps_total = total
        self._steps = 0

    @override
    def advance(self, amount: int = 1) -> None:
        if not self._steps_total:
            return
        self._steps += amount
        filled = min(self._steps / self._steps_total, 1.0)
        self._progress.update(self._task, completed=self._started - 1 + filled)

    def finish(self, description: str) -> None:
        self._progress.update(
            self._task, description=description, completed=self._phases
        )


@contextmanager
def loading(phases: int) -> Iterator[Reporter]:
    """Draw a phase-by-phase progress bar, then wipe it.

    ``transient`` clears the bar when the block exits — on the way to the TUI,
    which would otherwise start below a stale line, and on the way to an error
    message. Rendering to stderr keeps it off the stream Textual draws into.
    """
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    )
    with progress:
        reporter = _BarReporter(progress, progress.add_task("starting", total=phases), phases)
        yield reporter
        reporter.finish("done")
