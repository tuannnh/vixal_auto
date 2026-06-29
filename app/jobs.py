from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field

from app.pipeline import process

# Job lifecycle: queued -> running -> done | error
QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"


@dataclass
class JobSpec:
    """Everything the pipeline needs to drive one bot conversation."""

    kind: str                       # "image" | "video"
    inputs: list[str]               # local paths to the uploaded file(s)
    prompt: str = "Nude"            # image→image prompt option
    # video→video toggle settings (each is matched against a menu button):
    template: str = "1"             # 1..5
    breast: str = "small"           # small | medium | large
    duration: str = "5s"            # 5s | 10s | 15s | 20s | 25s | 30s (Gem cost rises!)
    ratio: str = "9:16"             # 9:16 | 16:9
    resolution: str = "480p"        # 480p | 720p

    def video_settings(self) -> dict[str, str]:
        return {
            "template": self.template, "breast": self.breast, "duration": self.duration,
            "ratio": self.ratio, "resolution": self.resolution,
        }


@dataclass
class Job:
    id: str
    kind: str
    status: str = QUEUED
    prompt: str = ""
    count: int = 0
    folder: str = ""
    progress: str = ""
    copytele_url: str = ""
    copytele_urls: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def public(self) -> dict:
        return asdict(self)


class JobManager:
    """In-memory queue with a single sequential worker.

    Jobs run one at a time — the bot conversation is inherently serial (one
    Telegram session, one menu state). The HTTP request returns immediately
    with a job id to poll. State is in-process: run a single uvicorn worker.
    """

    def __init__(self, max_jobs: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._specs: dict[str, JobSpec] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._max_jobs = max_jobs

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def enqueue(self, spec: JobSpec) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=spec.kind, prompt=spec.prompt)
        self._jobs[job.id] = job
        self._specs[job.id] = spec
        self._prune()
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def wait_for(self, job_id: str, timeout: float) -> Job | None:
        """Poll until the job leaves a non-terminal state or `timeout` elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self._jobs.get(job_id)
            if job is None or job.status in (DONE, ERROR):
                return job
            await asyncio.sleep(0.5)
        return self._jobs.get(job_id)

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.finished_at is not None),
            key=lambda j: j.finished_at,
        )
        for job in finished[: len(self._jobs) - self._max_jobs]:
            self._jobs.pop(job.id, None)
            self._specs.pop(job.id, None)

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            spec = self._specs.get(job_id)
            if job is None or spec is None:
                self._queue.task_done()
                continue

            def set_progress(msg: str, _j=job) -> None:
                _j.progress = msg

            job.status = RUNNING
            try:
                data = await process(spec, status=set_progress)
                job.count = data["count"]
                job.folder = data["folder"]
                job.copytele_urls = data["copytele_urls"]
                job.copytele_url = data["copytele_url"]
                job.progress = "done"
                job.status = DONE
            except Exception as exc:  # noqa: BLE001 - record any failure on the job
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = ERROR
            finally:
                job.finished_at = time.time()
                self._specs.pop(job_id, None)
                self._queue.task_done()


jobs = JobManager()
