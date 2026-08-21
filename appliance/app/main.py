from __future__ import annotations

import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.audiosocket import AudioSocketServer
from app.business import BusinessDirectory
from app.config import Settings
from app.db import Database
from app.knowledge import KnowledgeBase
from app.llm import LocalLLM
from app.notifications import TeamNotifier
from app.registry import CallRegistry
from app.stt import LocalSTT
from app.tts import LocalTTS
from app.voice_core import CallSession, VoiceCore

settings = Settings.from_env()
logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


class SimulatorRequest(BaseModel):
    call_uuid: str = ""
    text: str
    reset: bool = False


class Runtime:
    def __init__(self):
        self.database = Database(settings.database_path)
        self.business = BusinessDirectory(settings.service_area_path)
        self.knowledge = KnowledgeBase(settings.knowledge_dir)
        self.llm = LocalLLM(settings)
        self.stt = LocalSTT(settings)
        self.tts = LocalTTS(settings)
        self.registry = CallRegistry(settings.runtime_dir)
        self.notifier = TeamNotifier(settings, self.database)
        self.core = VoiceCore(settings, self.database, self.business, self.knowledge, self.llm, self.notifier)
        self.audio = AudioSocketServer(settings, self.core, self.stt, self.tts, self.registry)
        self.simulators: dict[str, CallSession] = {}
        self.ready = False

    async def start(self) -> None:
        await self.stt.start()
        await self.tts.start()
        if not await self.llm.health():
            raise RuntimeError("Local llama.cpp API is not ready")
        await self.tts.warm((
            self.core.greeting(),
            "I didn't catch that. Please say it once more.",
            "What name should I put this under?",
            "What's the best email for you? You can say skip.",
            "What's the full service address?",
        ))
        await self.audio.start()
        self.ready = True

    async def stop(self) -> None:
        self.ready = False
        await self.audio.stop()


runtime = Runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts) or ["*"])
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


def authorized(token: str | None) -> bool:
    return bool(token and secrets.compare_digest(token, settings.admin_token))


def require_admin(token: str | None) -> None:
    if not authorized(token):
        raise HTTPException(status_code=401, detail="Authentication required")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "floodman-voice-appliance"}


@app.get("/ready")
async def ready() -> JSONResponse:
    llm = await runtime.llm.health() if runtime.ready else False
    value = {"ready": bool(runtime.ready and llm), "llm": llm, "stt": runtime.stt._model is not None, "tts": runtime.tts._kokoro is not None}
    return JSONResponse(value, status_code=200 if value["ready"] else 503)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(token: str = Form(...)):
    if not secrets.compare_digest(token, settings.admin_token):
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("floodman_admin", token, httponly=True, samesite="strict", secure=settings.public_base_url.startswith("https://"), max_age=86400)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("floodman_admin")
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, floodman_admin: str | None = Cookie(default=None)):
    if not authorized(floodman_admin):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {"calls": runtime.database.list_calls(50), "settings": settings})


@app.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(call_id: int, request: Request, floodman_admin: str | None = Cookie(default=None)):
    if not authorized(floodman_admin):
        return RedirectResponse("/login", status_code=303)
    call = runtime.database.get_call(call_id)
    if not call:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "call.html", {"call": call})


@app.get("/simulator", response_class=HTMLResponse)
async def simulator(request: Request, floodman_admin: str | None = Cookie(default=None)):
    if not authorized(floodman_admin):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "simulator.html", {})


@app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics(request: Request, floodman_admin: str | None = Cookie(default=None)):
    if not authorized(floodman_admin):
        return RedirectResponse("/login", status_code=303)
    logs: dict[str, str] = {}
    for name in ("appliance.log", "asterisk-full.log", "asterisk-errors.log", "llama.log"):
        path = settings.log_dir / name
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-160:]
            logs[name] = "\n".join(lines)
    return templates.TemplateResponse(request, "diagnostics.html", {"logs": logs, "ready": runtime.ready})


@app.get("/api/calls")
async def api_calls(floodman_admin: str | None = Cookie(default=None)):
    require_admin(floodman_admin)
    return runtime.database.list_calls(100)


@app.post("/api/simulate")
async def api_simulate(payload: SimulatorRequest, floodman_admin: str | None = Cookie(default=None)):
    require_admin(floodman_admin)
    call_uuid = payload.call_uuid or f"sim-{uuid.uuid4()}"
    if payload.reset:
        runtime.simulators.pop(call_uuid, None)
        runtime.database.delete_call_by_uuid(call_uuid)
    session = runtime.simulators.get(call_uuid)
    if not session:
        session = runtime.core.create_session(call_uuid, "+12315550000", "+12319354921")
        runtime.simulators[call_uuid] = session
        greeting = runtime.core.greeting()
        runtime.database.add_message(session.call_id, "assistant", greeting)
        return {"call_uuid": call_uuid, "reply": greeting, "stage": session.state.stage, "snapshot": session.state.to_dict()}
    reply = await runtime.core.process(session, payload.text)
    if reply.end_call or reply.transfer_number:
        await runtime.core.disconnect(session, "simulation_completed")
    return {"call_uuid": call_uuid, "reply": reply.text, "stage": session.state.stage, "end_call": reply.end_call, "transfer_number": reply.transfer_number, "snapshot": session.state.to_dict()}
