import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from app.config import Settings
from app.provider.client import ProviderClient
from app.services.dispatch import SessionFactory
from app.worker.dispatcher import DispatchWorker


class FakeProviderClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def make_worker(
    process_intent: Callable[[SessionFactory, ProviderClient], Awaitable[bool]],
    provider_client: FakeProviderClient,
    *,
    polling_interval: float = 0.01,
) -> DispatchWorker:
    settings = Settings(
        environment="test",
        DISPATCH_POLLING_INTERVAL=polling_interval,
    )
    session_factory = cast(SessionFactory, object())
    return DispatchWorker(
        session_factory=session_factory,
        settings=settings,
        process_intent=process_intent,
        provider_client_factory=lambda _: cast(ProviderClient, provider_client),
    )


async def cancel_worker(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_processes_available_intents_sequentially_without_waiting() -> None:
    provider_client = FakeProviderClient()
    results = iter((True, True, False))
    calls: list[int] = []
    no_work_reached = asyncio.Event()

    async def process_intent(*_: Any) -> bool:
        calls.append(len(calls) + 1)
        result = next(results)
        if not result:
            no_work_reached.set()
        return result

    task = asyncio.create_task(make_worker(process_intent, provider_client).run())
    await asyncio.wait_for(no_work_reached.wait(), timeout=1)
    await cancel_worker(task)

    assert calls == [1, 2, 3]


@pytest.mark.anyio
async def test_waits_for_polling_interval_when_no_work_is_available() -> None:
    provider_client = FakeProviderClient()
    first_call = asyncio.Event()
    calls = 0

    async def process_intent(*_: Any) -> bool:
        nonlocal calls
        calls += 1
        first_call.set()
        return False

    task = asyncio.create_task(
        make_worker(process_intent, provider_client, polling_interval=10).run()
    )
    await asyncio.wait_for(first_call.wait(), timeout=1)
    await asyncio.sleep(0)

    assert calls == 1
    assert not task.done()
    await cancel_worker(task)


@pytest.mark.anyio
async def test_continues_after_unhandled_iteration_error() -> None:
    provider_client = FakeProviderClient()
    recovered = asyncio.Event()
    calls = 0

    async def process_intent(*_: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database temporarily unavailable")
        recovered.set()
        return False

    task = asyncio.create_task(make_worker(process_intent, provider_client).run())
    await asyncio.wait_for(recovered.wait(), timeout=1)
    await cancel_worker(task)

    assert calls == 2


@pytest.mark.anyio
async def test_cancellation_is_not_swallowed() -> None:
    provider_client = FakeProviderClient()
    iteration_started = asyncio.Event()

    async def process_intent(*_: Any) -> bool:
        iteration_started.set()
        await asyncio.Event().wait()
        return True

    task = asyncio.create_task(make_worker(process_intent, provider_client).run())
    await asyncio.wait_for(iteration_started.wait(), timeout=1)

    await cancel_worker(task)
    assert task.cancelled()


@pytest.mark.anyio
async def test_provider_client_is_closed_during_shutdown() -> None:
    provider_client = FakeProviderClient()
    iteration_started = asyncio.Event()

    async def process_intent(*_: Any) -> bool:
        iteration_started.set()
        await asyncio.Event().wait()
        return True

    task = asyncio.create_task(make_worker(process_intent, provider_client).run())
    await asyncio.wait_for(iteration_started.wait(), timeout=1)

    await cancel_worker(task)
    assert provider_client.closed is True
