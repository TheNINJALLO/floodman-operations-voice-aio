from app.intake import classify_service, normalize_confirmation, normalize_email, normalize_phone

def test_service_classification():
    assert classify_service("water is flooding my basement")["service_status"] == "supported"
    assert classify_service("I need roof repair")["service_status"] == "unsupported"
    assert classify_service("something unusual at the property")["service_status"] == "review"

def test_contact_normalizers():
    assert normalize_email("josh at example dot com") == "josh@example.com"
    assert normalize_phone("231 884 0943") == "+12318840943"
    assert normalize_confirmation("Yes, that's right") == "yes"
    assert normalize_confirmation("No, wrong") == "no"
