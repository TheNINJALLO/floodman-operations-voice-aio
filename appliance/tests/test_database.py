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
