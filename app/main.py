from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from . import auth, collaboration, jobs, problems, packages, folders, zoj
from .config import get_settings
from .db import Base, engine


@asynccontextmanager
async def lifespan(app):
    if get_settings().env != "production":
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="ZOJ Editor API", version="0.1.0", lifespan=lifespan,
              dependencies=[Depends(auth.check_browser_request)])


@app.exception_handler(HTTPException)
async def http_error(request, error):
    detail = error.detail if isinstance(error.detail, dict) else {"code": "http_error", "message": str(error.detail)}
    return JSONResponse({"error": detail}, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse({"error": {"code": "invalid_request", "message": "입력 값을 확인해 주세요.", "errors": str(error)}}, status_code=422)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/api/health")
def health():
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "service": "editor", "environment": get_settings().env}


app.include_router(auth.router)
app.include_router(folders.router)
app.include_router(problems.router)
app.include_router(collaboration.router)
app.include_router(jobs.router)

app.include_router(packages.router)
app.include_router(zoj.router)
