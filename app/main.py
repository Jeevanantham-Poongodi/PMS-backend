from contextlib import asynccontextmanager

import psycopg.errors
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import flows_routes, stock_routes
from app.api.v1.routes import router
from app.core import db
from app.core.config import get_settings
from app.core.errors import error_body, install_error_handlers
from app.core.logging import configure_logging, install_request_logging


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    db.close_pool()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title="PMS API", version="0.1.0", lifespan=lifespan)

    install_error_handlers(app)

    # Refusals raised by the database (RLS, privileges, check constraints) map to the envelope.
    @app.exception_handler(psycopg.errors.InsufficientPrivilege)
    async def _db_forbidden(request: Request, _: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content=error_body(request, "FORBIDDEN", "You do not have permission for this action."),
        )

    @app.exception_handler(psycopg.errors.CheckViolation)
    async def _db_check(request: Request, _: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(request, "VALIDATION_FAILED", "A value is not allowed."),
        )

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(request: Request, _: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=error_body(request, "DATABASE_UNAVAILABLE", "The database is unreachable."),
        )

    install_request_logging(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.include_router(router)
    app.include_router(flows_routes.router)
    app.include_router(stock_routes.router)
    return app


app = create_app()
