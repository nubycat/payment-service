import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.operations import router as operations_router
from app.api.receipts import router as receipts_router
from app.config import Settings, get_settings
from app.database import async_session_factory, close_engine
from app.logging_config import configure_logging
from app.worker.dispatcher import DispatchWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings: Settings = application.state.settings
    worker = DispatchWorker(
        session_factory=async_session_factory,
        settings=settings,
    )
    worker_task = asyncio.create_task(worker.run(), name="dispatch-worker")
    application.state.dispatch_worker_task = worker_task
    try:
        yield
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        await close_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    configure_logging(current_settings.log_level)

    application = FastAPI(
        title=current_settings.name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = current_settings
    application.include_router(health_router)
    application.include_router(operations_router)
    application.include_router(receipts_router)

    logger.info(
        "Application configured",
        extra={
            "event": "application_configured",
            "environment": current_settings.environment,
        },
    )
    return application


app = create_app()
