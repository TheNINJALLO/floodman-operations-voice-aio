from app.intake import classify_property_context, classify_service, normalize_confirmation, normalize_email, normalize_phone

def test_service_classification():
    assert classify_service("water is flooding my basement")["service_status"] == "supported"
    assert classify_service("I need roof repair")["service_status"] == "unsupported"
    assert classify_service("something unusual at the property")["service_status"] == "review"

def test_contact_normalizers():
    assert normalize_email("josh at example dot com") == "josh@example.com"
    assert normalize_phone("231 884 0943") == "+12318840943"
    assert normalize_confirmation("Yes, that's right") == "yes"
    assert normalize_confirmation("No, wrong") == "no"


def test_spelled_email_hyphens_are_treated_as_recognition_separators():
    assert normalize_email("j-o-s-h at example dot com") == "josh@example.com"
    assert normalize_email("j - o - s - h at example dot com") == "josh@example.com"
    assert normalize_email("J-O-A-C-H, S-H at gmail.com.") == "joachsh@gmail.com"


def test_real_email_hyphens_are_preserved():
    assert normalize_email("mary-jane at example dot com") == "mary-jane@example.com"
    assert normalize_email("mary dash jane at example dot com") == "mary-jane@example.com"
    assert normalize_email("mary hyphen jane at example dot com") == "mary-jane@example.com"


def test_property_context_is_reused_from_volunteered_details():
    assert classify_property_context("I have mold in my house") == "Residential property"
    assert classify_property_context("This is for our commercial office") == "Commercial or managed property"
    assert classify_property_context("There is water coming in") == ""
