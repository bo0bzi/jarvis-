"""Background jobs (e.g. installing a 3,000-skill pack): run without blocking the chat, report real progress,
and announce the result when done. Friday's instructions list running/recent jobs so 'update?' gets a true answer."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import activity
from .tools import tool


@dataclass
class Job:
    id: int
    title: str
    status: str = "running"  # running / done / failed
    progress: str = "starting"
    result: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    def line(self) -> str:
        secs = int((self.finished or time.time()) - self.started)
        if self.status == "running":
            return f"Job {self.id} '{self.title}': RUNNING for {secs}s - {self.progress}"
        return f"Job {self.id} '{self.title}': {self.status.upper()} after {secs}s - {self.result[:300]}"


_JOBS: list[Job] = []
_LOCK = threading.Lock()


def start(title: str, work: Callable[[Callable[[str], None]], str], notify: Callable[[str], None] | None = None,
          wait: bool = False) -> Job:
    """Run work(progress) in a thread. progress("1,200 of 2,955 files") updates the status line.
    notify(text) is called with the result when it finishes. wait=True runs it inline (tests)."""
    with _LOCK:
        job = Job(len(_JOBS) + 1, title)
        _JOBS.append(job)

    def progress(text: str) -> None:
        job.progress = text

    def run() -> None:
        try:
            job.result = str(work(progress))
            job.status = "failed" if job.result.lower().startswith("error") else "done"
        except Exception as e:  # never let a job kill Friday
            job.result, job.status = f"Error: {type(e).__name__}: {str(e)[:300]}", "failed"
        job.finished = time.time()
        activity.log("job", job.line())
        if notify:
            try:
                notify(f"Job finished - {job.title}: {job.result}")
            except Exception:
                pass

    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
    return job


def running() -> list[Job]:
    return [j for j in _JOBS if j.status == "running"]


def recent(n: int = 3) -> list[Job]:
    return _JOBS[-n:]


def status_text() -> str:
    if not _JOBS:
        return "No background jobs have run since Friday started."
    return "\n".join(j.line() for j in recent(5))


def prompt_section() -> str:
    jobs = recent(3)
    if not jobs:
        return ("\nBackground jobs: none running. Never say something is 'in progress', 'installing' or 'fetching' "
                "unless a tool you called this turn started it.")
    return "\nBackground jobs (the TRUE status - report from this, never guess):\n" + "\n".join(j.line() for j in jobs)


@tool("job_status", "The REAL status of background jobs (e.g. a skill pack install): running, done or failed, "
      "with progress. Use for 'update?', 'is it done?', 'status'.")
def job_status(ctx):
    return status_text()


def clear() -> None:  # tests
    with _LOCK:
        _JOBS.clear()
