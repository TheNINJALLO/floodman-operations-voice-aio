from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import Cookie, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.audiosocket import AudioSocketServer
from app.auth import AuthManager, Principal
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


class SimulatorRequest(BaseModel):
    call_uuid: str = ""
    text: str
    reset: bool = False


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)
    keys: dict[str, str]


class Runtime:
    def __init__(self):
        self.database = Database(settings.database_path)
        self.auth = AuthManager(self.database, settings.admin_token, settings.session_hours)
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
        await self.tts.warm(
            (
                self.core.greeting(),
                "I didn't catch that. Please say it once more.",
                "What name should I put this under?",
                "What's the best email for you? You can say skip.",
                "What's the full service address?",
            )
        )
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


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts) or ["*"])
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.url.scheme == "https" or settings.public_base_url.startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _login_allowed(key: str, *, failed: bool = False) -> bool:
    now = time.monotonic()
    attempts = login_attempts[key]
    while attempts and attempts[0] < now - 900:
        attempts.popleft()
    if failed:
        attempts.append(now)
    return len(attempts) < 8


def _principal(token: str | None) -> Principal | None:
    return runtime.auth.principal(token)


def _require_api(token: str | None, *, manage: bool = False, admin: bool = False) -> Principal:
    principal = _principal(token)
    if not principal:
        raise HTTPException(401, "Authentication required")
    if admin and not principal.is_admin:
        raise HTTPException(403, "Administrator access required")
    if manage and not principal.can_manage:
        raise HTTPException(403, "Manager access required")
    return principal


def _csrf(principal: Principal, value: str | None) -> None:
    try:
        runtime.auth.require_csrf(principal, value)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc


def _context(request: Request, principal: Principal, section: str, **values: Any) -> dict[str, Any]:
    unread = runtime.database.unread_notification_count(principal.user_id) if principal.user_id else 0
    return {
        "request": request,
        "principal": principal,
        "section": section,
        "csrf_token": principal.csrf_token,
        "unread_notifications": unread,
        "public_base_url": settings.public_base_url,
        **values,
    }


def _redirect(path: str, message: str = "", error: str = "") -> RedirectResponse:
    values = {key: value for key, value in (("message", message), ("error", error)) if value}
    target = path + (("?" if "?" not in path else "&") + urlencode(values) if values else "")
    return RedirectResponse(target, status_code=303)


def _audit(principal: Principal, action: str, entity_type: str, entity_id: str = "", detail: dict[str, Any] | None = None) -> None:
    runtime.database.audit(principal.user_id, principal.username, action, entity_type, entity_id, detail)


def _preferences(new_calls: str | None, completed: str | None, emergencies: str | None) -> dict[str, bool]:
    return {
        "notify_new_calls": new_calls is not None,
        "notify_completed_calls": completed is not None,
        "notify_emergencies": emergencies is not None,
    }


def _session_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        "floodman_session",
        token,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "floodman-voice-appliance"}


@app.get("/ready")
async def ready() -> JSONResponse:
    llm = await runtime.llm.health() if runtime.ready else False
    value = {"ready": bool(runtime.ready and llm), "llm": llm, "stt": runtime.stt._model is not None, "tts": runtime.tts._kokoro is not None}
    return JSONResponse(value, status_code=200 if value["ready"] else 503)


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# Authentication ------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, floodman_session: str | None = Cookie(default=None)):
    if _principal(floodman_session):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"user_count": runtime.database.user_count(), "error": request.query_params.get("error", ""), "message": request.query_params.get("message", "")},
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    recovery_token: str = Form(default=""),
):
    key = f"{request.client.host if request.client else 'unknown'}:{username.strip().lower()}"
    if not _login_allowed(key):
        return _redirect("/login", error="Too many attempts. Wait 15 minutes and try again.")
    session = runtime.auth.authenticate(username, password) if username or password else runtime.auth.authenticate_recovery(recovery_token)
    if not session:
        _login_allowed(key, failed=True)
        return _redirect("/login", error="The username, password, or recovery token was not accepted.")
    login_attempts.pop(key, None)
    token, _ = session
    destination = "/users" if runtime.database.user_count() == 0 else "/"
    response = RedirectResponse(destination, status_code=303)
    _session_cookie(response, token)
    return response


@app.post("/logout")
async def logout(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _principal(floodman_session)
    if principal:
        _csrf(principal, csrf_token)
    runtime.auth.logout(floodman_session)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("floodman_session", path="/")
    return response


# Dashboard and calls -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            principal,
            "calls",
            calls=runtime.database.list_calls(100),
            counts=runtime.database.dashboard_counts(),
            ready=runtime.ready,
        ),
    )


@app.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(call_id: int, request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    call = runtime.database.get_call(call_id)
    if not call:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "call.html",
        _context(request, principal, "calls", call=call, message=request.query_params.get("message", ""), error=request.query_params.get("error", "")),
    )


@app.post("/calls/{call_id}/edit")
async def edit_call(
    call_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    status: str = Form(default="completed"),
    outcome: str = Form(default=""),
    current_stage: str = Form(default=""),
    name: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
    address: str = Form(default=""),
    description: str = Form(default=""),
    service_key: str = Form(default=""),
    service_status: str = Form(default=""),
    service_area_status: str = Form(default=""),
    service_area_city: str = Form(default=""),
    property_context: str = Form(default=""),
    timing_summary: str = Form(default=""),
    safety_summary: str = Form(default=""),
    urgency: str = Form(default="normal"),
    department: str = Form(default="estimating"),
    completed: str | None = Form(default=None),
):
    principal = _require_api(floodman_session, manage=True)
    _csrf(principal, csrf_token)
    if status not in {"active", "completed"} or urgency not in {"normal", "emergency"} or department not in {"estimating", "emergency", "billing", "support"}:
        return _redirect(f"/calls/{call_id}", error="One of the selected call values is invalid.")
    values = {key: value for key, value in locals().items() if key in {
        "status", "outcome", "current_stage", "name", "email", "phone", "address", "description", "service_key",
        "service_status", "service_area_status", "service_area_city", "property_context", "timing_summary", "safety_summary", "urgency", "department"
    }}
    values["completed"] = completed is not None
    try:
        runtime.database.update_call(call_id, values)
        _audit(principal, "update", "call", str(call_id), {"fields": sorted(values)})
        return _redirect(f"/calls/{call_id}", message="Call details saved.")
    except (LookupError, ValueError) as exc:
        return _redirect(f"/calls/{call_id}", error=str(exc))


@app.post("/calls/{call_id}/delete")
async def delete_call(
    call_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session, admin=True)
    _csrf(principal, csrf_token)
    if not runtime.database.delete_call(call_id):
        return _redirect("/", error="Call not found.")
    _audit(principal, "delete", "call", str(call_id))
    return _redirect("/", message="Call and its related records were deleted.")


# Users and profile ---------------------------------------------------
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, edit: int | None = None, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    if not principal.is_admin:
        return _redirect("/", error="Administrator access is required.")
    selected = runtime.database.get_user(edit) if edit else None
    return templates.TemplateResponse(
        request,
        "users.html",
        _context(
            request,
            principal,
            "users",
            users=runtime.database.list_users(),
            selected=selected,
            needs_first_admin=runtime.database.user_count() == 0,
            message=request.query_params.get("message", ""),
            error=request.query_params.get("error", ""),
        ),
    )


@app.post("/users/create")
async def create_user(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    password: str = Form(default=""),
    role: str = Form(default="viewer"),
    notify_new_calls: str | None = Form(default=None),
    notify_completed_calls: str | None = Form(default=None),
    notify_emergencies: str | None = Form(default=None),
):
    principal = _require_api(floodman_session, admin=True)
    _csrf(principal, csrf_token)
    try:
        user_id = runtime.auth.create_user(
            username=username,
            display_name=display_name,
            password=password,
            role=role,
            preferences=_preferences(notify_new_calls, notify_completed_calls, notify_emergencies),
            actor=principal,
        )
        _audit(principal, "create", "user", str(user_id), {"username": username.strip().lower(), "role": role})
        return _redirect("/users", message="User account created.")
    except (ValueError, PermissionError, sqlite3.IntegrityError) as exc:
        message = "That username is already in use." if isinstance(exc, sqlite3.IntegrityError) else str(exc)
        return _redirect("/users", error=message)


@app.post("/users/{user_id}/edit")
async def edit_user(
    user_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    role: str = Form(default="viewer"),
    active: str | None = Form(default=None),
    notify_new_calls: str | None = Form(default=None),
    notify_completed_calls: str | None = Form(default=None),
    notify_emergencies: str | None = Form(default=None),
):
    principal = _require_api(floodman_session, admin=True)
    _csrf(principal, csrf_token)
    try:
        runtime.auth.update_user(
            user_id,
            username=username,
            display_name=display_name,
            role=role,
            active=active is not None,
            preferences=_preferences(notify_new_calls, notify_completed_calls, notify_emergencies),
            actor=principal,
        )
        _audit(principal, "update", "user", str(user_id), {"username": username.strip().lower(), "role": role, "active": active is not None})
        return _redirect("/users", message="User account updated.")
    except (ValueError, PermissionError, LookupError, sqlite3.IntegrityError) as exc:
        message = "That username is already in use." if isinstance(exc, sqlite3.IntegrityError) else str(exc)
        return _redirect(f"/users?edit={user_id}", error=message)


@app.post("/users/{user_id}/password")
async def change_user_password(
    user_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    password: str = Form(default=""),
):
    principal = _require_api(floodman_session)
    _csrf(principal, csrf_token)
    try:
        runtime.auth.set_password(user_id, password, principal)
        _audit(principal, "password_reset", "user", str(user_id))
        if principal.user_id == user_id:
            response = _redirect("/login", message="Password changed. Sign in again.")
            response.delete_cookie("floodman_session", path="/")
            return response
        return _redirect("/users", message="Password changed and the user's sessions were revoked.")
    except (ValueError, PermissionError, LookupError) as exc:
        target = "/profile" if principal.user_id == user_id else f"/users?edit={user_id}"
        return _redirect(target, error=str(exc))


@app.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session, admin=True)
    _csrf(principal, csrf_token)
    try:
        runtime.auth.delete_user(user_id, principal)
        _audit(principal, "delete", "user", str(user_id))
        return _redirect("/users", message="User account and its sessions were deleted.")
    except (ValueError, PermissionError, LookupError) as exc:
        return _redirect("/users", error=str(exc))


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    if principal.user_id is None:
        return _redirect("/users", error="Create a named user account before configuring a profile.")
    user = runtime.database.get_user(principal.user_id)
    return templates.TemplateResponse(
        request,
        "profile.html",
        _context(request, principal, "profile", user=user, message=request.query_params.get("message", ""), error=request.query_params.get("error", "")),
    )


@app.post("/profile")
async def update_profile(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    display_name: str = Form(default=""),
    notify_new_calls: str | None = Form(default=None),
    notify_completed_calls: str | None = Form(default=None),
    notify_emergencies: str | None = Form(default=None),
):
    principal = _require_api(floodman_session)
    _csrf(principal, csrf_token)
    if principal.user_id is None:
        return _redirect("/users", error="Create a named user account first.")
    user = runtime.database.get_user(principal.user_id)
    if not user:
        return _redirect("/login", error="User account not found.")
    runtime.database.update_user(
        principal.user_id,
        user["username"],
        str(display_name or "").strip()[:80] or user["display_name"],
        user["role"],
        bool(user["active"]),
        _preferences(notify_new_calls, notify_completed_calls, notify_emergencies),
    )
    _audit(principal, "update", "profile", str(principal.user_id))
    return _redirect("/profile", message="Profile and notification preferences saved.")


# Knowledge and service area -----------------------------------------
@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, edit: str = "", floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    selected = runtime.knowledge.managed_document(edit) if edit else None
    return templates.TemplateResponse(
        request,
        "knowledge.html",
        _context(
            request,
            principal,
            "knowledge",
            documents=runtime.knowledge.managed_documents(),
            selected=selected,
            message=request.query_params.get("message", ""),
            error=request.query_params.get("error", ""),
        ),
    )


@app.post("/knowledge/save")
async def save_knowledge(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    slug: str = Form(default=""),
    original_slug: str = Form(default=""),
    title: str = Form(default=""),
    category: str = Form(default="general"),
    tags: str = Form(default=""),
    body: str = Form(default=""),
    approved: str | None = Form(default=None),
):
    principal = _require_api(floodman_session, manage=True)
    _csrf(principal, csrf_token)
    try:
        saved = runtime.knowledge.save_document(
            slug=slug,
            original_slug=original_slug,
            title=title,
            category=category,
            tags=tags,
            body=body,
            approved=approved is not None,
        )
        _audit(principal, "save", "knowledge", saved, {"approved": approved is not None})
        return _redirect(f"/knowledge?edit={saved}", message="Knowledge document saved and reloaded.")
    except (ValueError, LookupError) as exc:
        return _redirect("/knowledge", error=str(exc))


@app.post("/knowledge/{slug}/delete")
async def delete_knowledge(
    slug: str,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session, admin=True)
    _csrf(principal, csrf_token)
    try:
        runtime.knowledge.delete_document(slug)
        _audit(principal, "delete", "knowledge", slug)
        return _redirect("/knowledge", message="Knowledge document deleted.")
    except (ValueError, LookupError) as exc:
        return _redirect("/knowledge", error=str(exc))


@app.get("/service-area", response_class=HTMLResponse)
async def service_area_page(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "service_area.html",
        _context(
            request,
            principal,
            "service-area",
            service_area=runtime.business.configuration(),
            message=request.query_params.get("message", ""),
            error=request.query_params.get("error", ""),
        ),
    )


@app.post("/service-area")
async def save_service_area(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
    state: str = Form(default=""),
    description: str = Form(default=""),
    cities: str = Form(default=""),
):
    principal = _require_api(floodman_session, manage=True)
    _csrf(principal, csrf_token)
    try:
        values = [value for value in re.split(r"[\r\n,]+", cities) if value.strip()]
        runtime.business.save_configuration(state, description, values)
        _audit(principal, "save", "service_area", state, {"city_count": len(values)})
        return _redirect("/service-area", message="Service area saved and reloaded.")
    except ValueError as exc:
        return _redirect("/service-area", error=str(exc))


# Notification center and browser push -------------------------------
@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    if principal.user_id is None:
        return _redirect("/users", error="Create a named user account to receive notifications.")
    subscriptions = [
        {key: row[key] for key in ("id", "user_agent", "created_at", "last_success_at", "failure_count")}
        for row in runtime.database.list_push_subscriptions(principal.user_id)
    ]
    return templates.TemplateResponse(
        request,
        "notifications.html",
        _context(
            request,
            principal,
            "notifications",
            notifications=runtime.database.list_user_notifications(principal.user_id),
            subscriptions=subscriptions,
            push_available=bool(runtime.notifier.web_push.public_key),
            push_secure=settings.public_base_url.startswith("https://"),
            message=request.query_params.get("message", ""),
            error=request.query_params.get("error", ""),
        ),
    )


@app.post("/notifications/read-all")
async def read_all_notifications(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session)
    _csrf(principal, csrf_token)
    if principal.user_id is not None:
        runtime.database.mark_notifications_read(principal.user_id)
    return _redirect("/notifications", message="Notifications marked as read.")


@app.post("/notifications/{notification_id}/delete")
async def delete_notification(
    notification_id: int,
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session)
    _csrf(principal, csrf_token)
    if principal.user_id is not None:
        runtime.database.delete_user_notification(principal.user_id, notification_id)
    return _redirect("/notifications", message="Notification removed.")


@app.post("/notifications/test")
async def test_notification(
    floodman_session: str | None = Cookie(default=None),
    csrf_token: str = Form(default=""),
):
    principal = _require_api(floodman_session)
    _csrf(principal, csrf_token)
    if principal.user_id is None:
        return _redirect("/users", error="Create a named user account first.")
    await runtime.notifier.web_push.publish_manual(
        principal.user_id,
        "Floodman notifications are ready",
        "This device can receive secure call alerts.",
    )
    return _redirect("/notifications", message="Test notification sent.")


@app.get("/api/push/config")
async def push_config(floodman_session: str | None = Cookie(default=None)):
    principal = _require_api(floodman_session)
    return {
        "enabled": principal.user_id is not None,
        "public_key": runtime.notifier.web_push.public_key if principal.user_id is not None else "",
        "secure_context": settings.public_base_url.startswith("https://"),
    }


@app.post("/api/push/subscriptions")
async def subscribe_push(
    payload: PushSubscriptionRequest,
    request: Request,
    floodman_session: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
):
    principal = _require_api(floodman_session)
    _csrf(principal, x_csrf_token)
    if principal.user_id is None:
        raise HTTPException(400, "Named user account required")
    parsed = urlparse(payload.endpoint)
    p256dh = str(payload.keys.get("p256dh") or "")
    auth = str(payload.keys.get("auth") or "")
    if parsed.scheme != "https" or not parsed.netloc or not p256dh or not auth or len(p256dh) > 512 or len(auth) > 512:
        raise HTTPException(400, "Invalid browser push subscription")
    runtime.database.upsert_push_subscription(
        principal.user_id,
        payload.endpoint,
        p256dh,
        auth,
        request.headers.get("user-agent", ""),
    )
    _audit(principal, "subscribe", "push_device")
    return {"ok": True}


@app.delete("/api/push/subscriptions")
async def unsubscribe_push(
    payload: dict[str, Any],
    floodman_session: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
):
    principal = _require_api(floodman_session)
    _csrf(principal, x_csrf_token)
    if principal.user_id is not None:
        runtime.database.delete_push_subscription(principal.user_id, endpoint=str(payload.get("endpoint") or ""))
        _audit(principal, "unsubscribe", "push_device")
    return {"ok": True}


@app.get("/api/notifications/unread")
async def unread_notifications(floodman_session: str | None = Cookie(default=None)):
    principal = _require_api(floodman_session)
    return {"count": runtime.database.unread_notification_count(principal.user_id) if principal.user_id else 0}


# Diagnostics, audit, and simulator ----------------------------------
@app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    logs: dict[str, str] = {}
    for name in ("appliance.log", "asterisk-full.log", "asterisk-errors.log", "llama.log"):
        path = settings.log_dir / name
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-160:]
            logs[name] = "\n".join(lines)
    return templates.TemplateResponse(request, "diagnostics.html", _context(request, principal, "diagnostics", logs=logs, ready=runtime.ready))


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    if not principal.is_admin:
        return _redirect("/", error="Administrator access is required.")
    return templates.TemplateResponse(request, "audit.html", _context(request, principal, "audit", events=runtime.database.list_audit_events()))


@app.get("/simulator", response_class=HTMLResponse)
async def simulator(request: Request, floodman_session: str | None = Cookie(default=None)):
    principal = _principal(floodman_session)
    if not principal:
        return RedirectResponse("/login", status_code=303)
    if not principal.can_manage:
        return _redirect("/", error="Manager access is required to run simulations.")
    return templates.TemplateResponse(request, "simulator.html", _context(request, principal, "simulator"))


@app.get("/api/calls")
async def api_calls(floodman_session: str | None = Cookie(default=None)):
    _require_api(floodman_session)
    return runtime.database.list_calls(100)


@app.post("/api/simulate")
async def api_simulate(
    payload: SimulatorRequest,
    floodman_session: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
):
    principal = _require_api(floodman_session, manage=True)
    _csrf(principal, x_csrf_token)
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
    return {
        "call_uuid": call_uuid,
        "reply": reply.text,
        "stage": session.state.stage,
        "end_call": reply.end_call,
        "transfer_number": reply.transfer_number,
        "snapshot": session.state.to_dict(),
    }
