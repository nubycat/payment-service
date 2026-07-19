import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest
from app.config import Settings
from app.main import lifespan
from fastapi import FastAPI


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_recovery_finishes_before_worker_task_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_recovery(*_: Any) -> int:
        events.append("recovery")
        return 0

    class FakeWorker:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def run(self) -> None:
            await asyncio.Event().wait()

    def fake_create_task(
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Future[None]:
        assert name == "dispatch-worker"
        events.append("worker_task_created")
        coroutine.close()
        return asyncio.get_running_loop().create_future()

    async def fake_close_engine() -> None:
        pass

    monkeypatch.setattr("app.main.recover_submission_intents", fake_recovery)
    monkeypatch.setattr("app.main.DispatchWorker", FakeWorker)
    monkeypatch.setattr("app.main.asyncio.create_task", fake_create_task)
    monkeypatch.setattr("app.main.close_engine", fake_close_engine)

    application = FastAPI()
    application.state.settings = Settings(environment="test")

    async with lifespan(application):
        assert events == ["recovery", "worker_task_created"]
