import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.provider.client import ProviderClient
from app.services.dispatch import SessionFactory, process_one_intent

logger = logging.getLogger(__name__)

type ProcessOneIntent = Callable[[SessionFactory, ProviderClient], Awaitable[bool]]
type ProviderClientFactory = Callable[[Settings], ProviderClient]


def _create_provider_client(settings: Settings) -> ProviderClient:
    return ProviderClient.from_settings(settings)


class DispatchWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        *,
        process_intent: ProcessOneIntent = process_one_intent,
        provider_client_factory: ProviderClientFactory = _create_provider_client,
    ) -> None:
        self._session_factory = session_factory
        self._polling_interval = settings.dispatch_polling_interval_seconds
        self._process_intent = process_intent
        self._provider_client_factory = provider_client_factory
        self._settings = settings

    async def run(self) -> None:
        provider_client = self._provider_client_factory(self._settings)
        logger.info("Dispatch worker started", extra={"event": "dispatch_worker_started"})
        try:
            while True:
                try:
                    processed = await self._process_intent(
                        self._session_factory,
                        provider_client,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Dispatch iteration failed",
                        extra={"event": "dispatch_iteration_failed"},
                    )
                    await asyncio.sleep(self._polling_interval)
                    continue

                if not processed:
                    await asyncio.sleep(self._polling_interval)
        finally:
            await provider_client.close()
            logger.info("Dispatch worker stopped", extra={"event": "dispatch_worker_stopped"})
