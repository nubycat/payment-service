import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    configure_logging(current_settings.log_level)

    application = FastAPI(title=current_settings.name, version="0.1.0")
    application.state.settings = current_settings
    application.include_router(health_router)

    logger.info(
        "Application configured",
        extra={
            "event": "application_configured",
            "environment": current_settings.environment,
        },
    )
    return application


app = create_app()
