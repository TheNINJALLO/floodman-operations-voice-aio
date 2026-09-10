from __future__ import annotations

from pathlib import Path
import importlib
import asyncio
import json
import re
import sys

import numpy as np
import pytest
from jinja2 import Environment, FileSystemLoader
from fastapi.testclient import TestClient

from app.auth import AuthManager, Principal, hash_password, verify_password
from app.business import BusinessDirectory
from app.config import Settings
from app.db import Database
from app.emailer import EmailService
from app.knowledge import KnowledgeBase
from app.models import IntakeState
from app.notifications import TeamNotifier
from app.tts import LocalTTS


def preferences(**overrides: bool) -> dict[str, bool]:
    values = {
        "notify_new_calls": True,
        "notify_completed_calls": True,
        "notify_emergencies": True,
        "email_notifications": False,
    }
    values.update(overrides)
    return values


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("Strong password 47!")
    second = hash_password("Strong password 47!")
    assert first != second
    assert verify_password("Strong password 47!", first)
    assert not verify_password("wrong password", first)
    assert "Strong password 47!" not in first
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("TooShort1!")


def test_user_roles_sessions_and_last_admin_protection(tmp_path: Path) -> None:
    database = Database(tmp_path / "portal.db")
    auth = AuthManager(database, "recovery-secret", 12)
    recovery_session = auth.authenticate_recovery("recovery-secret")
    assert recovery_session is not None
    recovery = auth.principal(recovery_session[0])
    assert recovery and recovery.is_admin and recovery.is_recovery

    admin_id = auth.create_user(
        username="first.admin",
        display_name="First Admin",
        password="Admin password 47!",
        role="admin",
        preferences=preferences(),
        actor=recovery,
    )
    manager_id = auth.create_user(
        username="call.manager",
        display_name="Call Manager",
        password="Manager password 47!",
        role="manager",
        preferences=preferences(),
        actor=recovery,
        email="manager@example.com",
    )
    admin_session = auth.authenticate("FIRST.ADMIN", "Admin password 47!")
    assert admin_session is not None
    admin = auth.principal(admin_session[0])
    assert admin and admin.user_id == admin_id and admin.can_manage

    with pytest.raises(ValueError, match="administrator must remain"):
        auth.update_user(
            admin_id,
            username="first.admin",
            display_name="First Admin",
            role="viewer",
            active=True,
            preferences=preferences(),
            actor=admin,
        )
    with pytest.raises(ValueError, match="own account"):
        auth.delete_user(admin_id, admin)

    auth.set_password(manager_id, "Updated password 47!", admin)
    assert auth.authenticate("call.manager", "Manager password 47!") is None
    assert auth.authenticate("call.manager", "Updated password 47!") is not None


def test_notifications_are_preference_scoped_and_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "notifications.db")
    admin_id = database.create_user("alerts.on", "Alerts On", "hash", "admin", preferences(), None)
    quiet_id = database.create_user(
        "alerts.off",
        "Alerts Off",
        "hash",
        "viewer",
        preferences(notify_new_calls=False, notify_completed_calls=False, notify_emergencies=True),
        admin_id,
    )
    records = database.create_user_notifications(event_key="call:started", kind="call_started", title="Incoming", body="Open portal", url="/calls/1")
    assert [record["user_id"] for record in records] == [admin_id]
    assert database.create_user_notifications(event_key="call:started", kind="call_started", title="Incoming", body="Open portal", url="/calls/1") == []
    emergency = database.create_user_notifications(event_key="call:emergency", kind="emergency", title="Emergency", body="Open portal", url="/calls/1")
    assert {record["user_id"] for record in emergency} == {admin_id, quiet_id}
    manual = database.create_user_notifications(event_key="manual:quiet", kind="manual", title="Test", body="Ready", url="/notifications", target_user_id=quiet_id)
    assert len(manual) == 1
    assert database.unread_notification_count(quiet_id) == 2
    database.mark_notifications_read(quiet_id)
    assert database.unread_notification_count(quiet_id) == 0


def test_call_records_can_be_edited_and_deleted(tmp_path: Path) -> None:
    database = Database(tmp_path / "calls.db")
    call_id = database.create_call(IntakeState(call_uuid="editable-call", name="Old name"))
    database.add_message(call_id, "caller", "hello")
    database.update_call(call_id, {"status": "completed", "outcome": "follow_up", "name": "New name", "urgency": "emergency", "completed": True})
    call = database.get_call(call_id)
    assert call and call["status"] == "completed" and call["snapshot"]["name"] == "New name"
    assert call["snapshot"]["urgency"] == "emergency" and call["intake_completed"] == 1
    assert database.dashboard_counts() == {"total": 1, "active": 0, "emergencies": 1, "completed": 1}
    assert database.delete_call(call_id)
    assert database.get_call(call_id) is None


def test_managed_knowledge_and_service_area_reload_atomically(tmp_path: Path) -> None:
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    slug = knowledge.save_document(slug="payment-options", title="Payment Options", category="billing", tags="payments, financing", body="Ask the team about available payment options.", approved=True)
    assert slug == "payment-options"
    assert knowledge.search("financing")[0].title == "Payment Options"
    renamed = knowledge.save_document(slug="financing-options", original_slug=slug, title="Financing", category="billing", tags="financing", body="Financing requires team confirmation.", approved=False)
    assert renamed == "financing-options" and knowledge.documents == []
    knowledge.delete_document(renamed)
    assert knowledge.managed_documents() == []

    path = tmp_path / "service-area.yaml"
    directory = BusinessDirectory(path)
    directory.save_configuration("Michigan", "West Michigan", ["Ludington", "Grand Rapids", "Ludington"])
    assert directory.configuration()["cities"] == ["Grand Rapids", "Ludington"]
    assert directory.service_area("Grand Rapids, Michigan").status == "published"


def test_all_admin_templates_parse(project_root: Path) -> None:
    root = project_root / "templates"
    environment = Environment(loader=FileSystemLoader(root), autoescape=True)
    for path in sorted(root.glob("*.html")):
        environment.get_template(path.name)


def test_push_worker_and_security_contracts(project_root: Path) -> None:
    worker = (project_root / "static/service-worker.js").read_text(encoding="utf-8")
    client = (project_root / "static/app.js").read_text(encoding="utf-8")
    main = (project_root / "app/main.py").read_text(encoding="utf-8")
    assert 'self.addEventListener("push"' in worker
    assert "showNotification" in worker and "notificationclick" in worker
    assert "Notification.requestPermission()" in client
    assert '"X-CSRF-Token": csrf' in client
    assert "Content-Security-Policy" in main and "samesite=\"strict\"" in main


@pytest.mark.asyncio
async def test_installed_voice_catalog_preview_and_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    tts = LocalTTS(settings)

    class FakeKokoro:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        @staticmethod
        def get_voices() -> list[str]:
            return ["ef_dora", "bm_george", "af_heart", "am_liam"]

        def create(self, text: str, **values: object):
            self.calls.append({"text": text, **values})
            return np.full(240, .05, dtype=np.float32), 24000

    fake = FakeKokoro()
    tts._kokoro = fake
    assert await tts.available_voices() == ["af_heart", "am_liam", "bm_george"]
    preview = await tts.preview("Thanks for calling.", "bm_george", 1.1)
    assert preview and fake.calls[-1]["lang"] == "en-gb"
    assert fake.calls[-1]["voice"] == "bm_george"

    assert await tts.configure("bm_george", 1.1) == ("bm_george", 1.1)
    saved = json.loads(settings.voice_settings_path.read_text(encoding="utf-8"))
    assert saved == {"voice": "bm_george", "speed": 1.1}
    restarted = Settings.from_env()
    assert restarted.kokoro_voice == "bm_george" and restarted.kokoro_speed == 1.1
    with pytest.raises(ValueError, match="installed English voice"):
        await tts.configure("ef_dora", 1.0)
    with pytest.raises(ValueError, match="between 0.75 and 1.25"):
        await tts.preview("Hello", "af_heart", 1.5)


@pytest.mark.asyncio
async def test_incoming_push_never_delays_the_call_and_contains_no_customer_pii(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    database = Database(settings.database_path)
    user_id = database.create_user("alert.admin", "Alert Admin", "hash", "admin", preferences(), None)
    notifier = TeamNotifier(settings, database)
    gate = asyncio.Event()

    async def slow_send(records: list[dict[str, object]]) -> None:
        await gate.wait()

    monkeypatch.setattr(notifier.web_push, "send_records", slow_send)
    state = IntakeState(call_uuid="privacy-call", name="Sensitive Customer", phone="+12315550100")
    call_id = database.create_call(state)
    assert await asyncio.wait_for(notifier.call_started(call_id, state), timeout=.1) == 0
    await asyncio.sleep(0)
    records = database.list_user_notifications(user_id)
    assert len(records) == 1
    assert "Sensitive Customer" not in records[0]["title"] + records[0]["body"]
    assert "+12315550100" not in records[0]["title"] + records[0]["body"]
    gate.set()
    await asyncio.gather(*notifier._background_tasks)


def test_recovery_bootstrap_and_named_login_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TOKEN", "test-recovery-token")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    client = TestClient(module.app, base_url="https://testserver")

    assert client.get("/").status_code == 200
    assert client.get("/").history[-1].headers["location"] == "/login"
    login = client.post("/login", data={"recovery_token": "test-recovery-token"}, follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/users"
    users_page = client.get("/users")
    assert "Add a team member" in users_page.text
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', users_page.text)
    assert csrf
    created = client.post(
        "/users/create",
        data={
            "csrf_token": csrf.group(1),
            "username": "portal.admin",
            "display_name": "Portal Admin",
            "email": "portal.admin@example.com",
            "password": "Portal password 47!",
            "role": "admin",
            "email_notifications": "on",
            "notify_new_calls": "on",
            "notify_completed_calls": "on",
            "notify_emergencies": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    client.cookies.clear()
    named_login = client.post(
        "/login",
        data={"username": "portal.admin", "password": "Portal password 47!"},
        follow_redirects=False,
    )
    assert named_login.status_code == 303 and named_login.headers["location"] == "/"
    dashboard = client.get("/")
    assert dashboard.status_code == 200 and "Calls and intake" in dashboard.text
    named_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', dashboard.text)
    assert named_csrf
    async def installed_voices() -> list[str]:
        return ["af_heart", "am_liam", "bf_emma", "bm_george"]
    async def preview_audio(text: str, voice: str, speed: float) -> bytes:
        return b"\x00\x00" * 160
    monkeypatch.setattr(module.runtime.tts, "available_voices", installed_voices)
    monkeypatch.setattr(module.runtime.tts, "preview", preview_audio)
    voice_preview = client.post(
        "/api/voice/preview",
        headers={"X-CSRF-Token": named_csrf.group(1)},
        json={"voice": "bf_emma", "speed": 1.0, "text": "Hello from Floodman."},
    )
    assert voice_preview.status_code == 200 and voice_preview.headers["content-type"] == "audio/wav"
    assert voice_preview.content.startswith(b"RIFF")
    notifications = client.get("/notifications")
    assert notifications.status_code == 200 and "Get call alerts on this device" in notifications.text
    call_id = module.runtime.database.create_call(IntakeState(call_uuid="route-test", name="Route Test"))
    for path in (
        f"/calls/{call_id}",
        "/users",
        "/users?edit=1",
        "/profile",
        "/email-settings",
        "/knowledge",
        "/service-area",
        "/notifications",
        "/audit",
        "/voice",
        "/diagnostics",
        "/simulator",
        "/sms-program",
        "/terms",
        "/privacy",
    ):
        response = client.get(path)
        assert response.status_code == 200, path

    profile = client.get("/profile")
    assert "previously unchecked" not in profile.text
    assert "Reply STOP" in profile.text and 'name="sms_notifications"' in profile.text
    opted_in = client.post(
        "/profile",
        data={
            "csrf_token": named_csrf.group(1),
            "display_name": "Portal Admin",
            "email": "portal.admin@example.com",
            "phone": "(231) 555-0100",
            "email_notifications": "on",
            "sms_notifications": "on",
            "notify_new_calls": "on",
            "notify_completed_calls": "on",
            "notify_emergencies": "on",
        },
        follow_redirects=False,
    )
    assert opted_in.status_code == 303
    enrolled = module.runtime.database.get_user(1)
    assert enrolled and enrolled["phone"] == "+12315550100" and enrolled["sms_notifications"] == 1
    assert module.runtime.database.list_sms_consent_events(1)[0]["action"] == "opt_in"


@pytest.mark.asyncio
async def test_email_settings_persist_mask_secrets_and_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    service = EmailService(settings)
    configuration = service.configure(
        {
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "username": "mailer@example.com",
            "password": "provider-secret",
            "from_email": "notifications@example.com",
            "from_name": "Floodman Alerts",
            "timeout_seconds": 8,
        }
    )
    assert configuration.configured
    assert service.configuration.public_dict()["password_configured"] is True
    assert "password" not in service.configuration.public_dict()
    assert "provider-secret" not in json.dumps(service.configuration.public_dict())

    sent: list[tuple[str, str, str]] = []

    def capture_send(current, recipient: str, subject: str, body: str) -> None:
        assert current.password == "provider-secret"
        sent.append((recipient, subject, body))

    monkeypatch.setattr(service, "_send_sync", capture_send)
    status, response = await service.send("manager@example.com", "Call ready", "Open the dashboard")
    assert status == "sent" and "accepted" in response.lower()
    assert sent == [("manager@example.com", "Call ready", "Open the dashboard")]

    restarted = EmailService(settings)
    assert restarted.configuration.password == "provider-secret"
    restarted.configure({**restarted.configuration.public_dict(), "password": "", "port": 465, "security": "tls"})
    assert restarted.configuration.password == "provider-secret" and restarted.configuration.port == 465


@pytest.mark.asyncio
async def test_email_call_alerts_are_permission_scoped_idempotent_and_nonblocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    database = Database(settings.database_path)
    enabled_preferences = preferences(email_notifications=True)
    allowed_id = database.create_user(
        "email.on", "Email On", "hash", "manager", enabled_preferences, None, "allowed@example.com"
    )
    database.create_user(
        "email.off",
        "Email Off",
        "hash",
        "viewer",
        preferences(email_notifications=False),
        allowed_id,
        "disabled@example.com",
    )
    database.create_user(
        "event.off",
        "Event Off",
        "hash",
        "admin",
        preferences(email_notifications=True, notify_completed_calls=False),
        allowed_id,
        "quiet@example.com",
    )
    notifier = TeamNotifier(settings, database)
    gate = asyncio.Event()
    delivered: list[tuple[str, str, str]] = []

    async def slow_email(recipient: str, subject: str, body: str) -> tuple[str, str]:
        delivered.append((recipient, subject, body))
        await gate.wait()
        return "sent", "accepted"

    monkeypatch.setattr(notifier.email, "send", slow_email)
    state = IntakeState(
        call_uuid="email-call",
        name="Customer Name",
        phone="+12315550100",
        address="1 Main Street",
        description="Water in basement",
    )
    call_id = database.create_call(state)
    count = await asyncio.wait_for(
        notifier.send(call_id, state, kind="completed_intake", partial=False),
        timeout=.1,
    )
    assert count == 3  # Two in-app records and one permission-scoped email.
    assert await notifier.send(call_id, state, kind="completed_intake", partial=False) == 0
    await asyncio.sleep(0)
    attempts = database.get_call(call_id)["notifications"]
    assert [(item["channel"], item["recipient"], item["status"]) for item in attempts] == [
        ("email", "allowed@example.com", "queued")
    ]
    for _ in range(10):
        if delivered:
            break
        await asyncio.sleep(0)
    gate.set()
    await notifier.stop()
    assert delivered[0][0] == "allowed@example.com"
    assert "Customer Name" in delivered[0][2] and "/calls/" in delivered[0][2]
    assert database.get_call(call_id)["notifications"][0]["status"] == "sent"


@pytest.mark.asyncio
async def test_sms_call_alerts_require_self_consent_and_exclude_customer_pii(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.example.com")
    settings = Settings.from_env()
    database = Database(settings.database_path)
    user_id = database.create_user("sms.on", "SMS On", "hash", "manager", preferences(), None)
    database.update_sms_consent(
        user_id,
        "+12315550100",
        True,
        source="authenticated_profile",
        disclosure_version="test-v1",
        disclosure_text="Test disclosure",
    )
    database.create_user("sms.off", "SMS Off", "hash", "viewer", preferences(), user_id)
    notifier = TeamNotifier(settings, database)
    delivered: list[tuple[str, str]] = []

    async def capture_sms(recipient: str, body: str) -> tuple[str, str]:
        delivered.append((recipient, body))
        return "queued", "accepted"

    monkeypatch.setattr(notifier, "_send_sms", capture_sms)
    state = IntakeState(
        call_uuid="sms-call",
        name="Sensitive Customer",
        phone="+12315550199",
        email="sensitive@example.com",
        address="1 Private Street",
    )
    call_id = database.create_call(state)
    assert await notifier.send(call_id, state, kind="emergency") == 3  # Two in-app records and one opted-in SMS.
    await notifier.stop()

    assert delivered[0][0] == "+12315550100"
    body = delivered[0][1]
    for private_value in (state.name, state.phone, state.email, state.address, state.call_uuid):
        assert private_value not in body
    assert "Floodman Call Center" in body and f"/calls/{call_id}" in body and "STOP" in body
