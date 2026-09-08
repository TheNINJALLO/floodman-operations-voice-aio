from __future__ import annotations

from pathlib import Path
import importlib
import asyncio
import re
import sys

import pytest
from jinja2 import Environment, FileSystemLoader
from fastapi.testclient import TestClient

from app.auth import AuthManager, Principal, hash_password, verify_password
from app.business import BusinessDirectory
from app.config import Settings
from app.db import Database
from app.knowledge import KnowledgeBase
from app.models import IntakeState
from app.notifications import TeamNotifier


def preferences(**overrides: bool) -> dict[str, bool]:
    values = {
        "notify_new_calls": True,
        "notify_completed_calls": True,
        "notify_emergencies": True,
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
async def test_incoming_push_never_delays_the_call_and_contains_no_customer_pii(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    database = Database(settings.database_path)
    user_id = database.create_user("alert.admin", "Alert Admin", "hash", "admin", preferences(), None)
    notifier = TeamNotifier(settings, database)
    gate = asyncio.Event()

    async def slow_publish(call_id: int, state: IntakeState, kind: str) -> int:
        records = database.create_user_notifications(event_key="privacy-test", kind=kind, title="Incoming Floodman call", body="Open the secure dashboard for details.", url=f"/calls/{call_id}", call_id=call_id)
        await gate.wait()
        return len(records)

    monkeypatch.setattr(notifier.web_push, "publish_call", slow_publish)
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
            "password": "Portal password 47!",
            "role": "admin",
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
    notifications = client.get("/notifications")
    assert notifications.status_code == 200 and "Get call alerts on this device" in notifications.text
    call_id = module.runtime.database.create_call(IntakeState(call_uuid="route-test", name="Route Test"))
    for path in (
        f"/calls/{call_id}",
        "/users",
        "/users?edit=1",
        "/profile",
        "/knowledge",
        "/service-area",
        "/notifications",
        "/audit",
        "/diagnostics",
        "/simulator",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
