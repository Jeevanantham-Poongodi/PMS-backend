# Entry point used by the Dockerfile (`uvicorn main:app`). The application lives in app/.
from app.main import app

__all__ = ["app"]
