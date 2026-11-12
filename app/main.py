from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1 import (
    auth,
    clients,
    documents,
    feedback,
    health,
    inputs,
    projects,
    requirements,
    srs,
    webhooks,
    workflow,
)
from app.core.config import get_settings
from app.core.envelope import fail
from app.core.errors import AppError
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_for_startup()
    configure_logging(settings.log_level)
    scheduler = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal scheduler
        if settings.local_scheduler and not settings.app_env == "test":
            from app.workers.local_scheduler import LocalScheduler

            scheduler = LocalScheduler(settings)
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.stop()

    app = FastAPI(title="Agentic SRS System", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content=fail(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content=fail(
            "validation_failed", "Invalid request", {"errors": errors}))

    prefix = "/api/v1"
    for module in (auth, clients, projects, documents, requirements, srs, feedback, inputs, webhooks,
                   workflow, health):
        app.include_router(module.router, prefix=prefix)
    return app


app = create_app()


def run() -> None:
    """`python main.py`: prepare the database, migrate, then serve the API with the scheduler."""
    import os

    import uvicorn
    from dotenv import load_dotenv

    from app.core import bootstrap

    load_dotenv(bootstrap.ROOT / ".env")
    bootstrap.prepare_database()
    get_settings.cache_clear()
    settings = get_settings()
    bootstrap.migrate()
    bootstrap.prepare_workflow_tables(settings)
    bootstrap.ensure_admin()
    bootstrap.report(settings)
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
