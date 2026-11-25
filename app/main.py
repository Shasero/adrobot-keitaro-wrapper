from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker

from app.api import campaigns
from app.api.health import router as health_router
from app.core.config import Settings
from app.db.session import create_db_engine
from app.services.campaigns import CampaignError

APP_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    application.state.session_factory = sessionmaker(bind=engine)
    application.state.settings = settings
    application.add_exception_handler(CampaignError, campaigns.campaign_error)
    application.add_exception_handler(RequestValidationError, campaigns.invalid_request)
    application.include_router(campaigns.router)
    application.include_router(health_router)
    application.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=APP_DIR / "templates")

    @application.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="index.html", context={"app_name": settings.app_name}
        )

    @application.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def docs(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="docs.html", context={"schema": application.openapi()}
        )

    return application


app = create_app()
