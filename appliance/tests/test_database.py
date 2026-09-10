from app.db import Database
from app.models import IntakeState

def test_database_cascade_and_notification(tmp_path):
    db = Database(tmp_path / "floodman.db")
    state = IntakeState(call_uuid="call-1", name="Josh")
    call_id = db.create_call(state)
    db.add_message(call_id, "caller", "hello")
    assert db.record_notification(call_id, "lead", "+12315550000", "queued", "ok", "key")
    assert not db.record_notification(call_id, "lead", "+12315550000", "queued", "ok", "key")
    assert db.get_call(call_id)["messages"][0]["text"] == "hello"
    db.delete_call_by_uuid("call-1")
    assert db.get_call(call_id) is None


def test_database_adds_email_delivery_columns(tmp_path):
    db = Database(tmp_path / "email-migration.db")
    with db.connect() as connection:
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        notification_columns = {row["name"] for row in connection.execute("PRAGMA table_info(notifications)")}
    assert {
        "email",
        "email_notifications",
        "phone",
        "sms_notifications",
        "sms_consent_at",
        "sms_consent_source",
        "sms_consent_version",
    } <= user_columns
    assert "channel" in notification_columns


def test_sms_consent_is_explicit_auditable_and_permission_scoped(tmp_path):
    db = Database(tmp_path / "sms-consent.db")
    preferences = {
        "notify_new_calls": True,
        "notify_completed_calls": True,
        "notify_emergencies": False,
        "email_notifications": False,
    }
    user_id = db.create_user("sms.user", "SMS User", "hash", "manager", preferences, None)

    assert db.list_sms_notification_recipients("completed_intake") == []
    db.update_sms_consent(
        user_id,
        "+12315550100",
        True,
        source="authenticated_profile",
        disclosure_version="test-v1",
        disclosure_text="Test consent disclosure",
    )
    assert [row["id"] for row in db.list_sms_notification_recipients("completed_intake")] == [user_id]
    assert db.list_sms_notification_recipients("emergency") == []

    db.update_sms_consent(
        user_id,
        "+12315550101",
        True,
        source="authenticated_profile",
        disclosure_version="test-v1",
        disclosure_text="Test consent disclosure",
    )
    db.update_sms_consent(
        user_id,
        "+12315550101",
        False,
        source="authenticated_profile",
        disclosure_version="test-v1",
        disclosure_text="Test consent disclosure",
    )
    events = db.list_sms_consent_events(user_id)
    assert [(row["phone"], row["action"]) for row in events] == [
        ("+12315550100", "opt_in"),
        ("+12315550100", "opt_out"),
        ("+12315550101", "opt_in"),
        ("+12315550101", "opt_out"),
    ]
    assert db.list_sms_notification_recipients("completed_intake") == []
