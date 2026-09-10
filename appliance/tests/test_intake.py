from app.intake import classify_property_context, classify_service, detect_emergency, normalize_confirmation, normalize_email, normalize_name, normalize_phone, spoken_address, spoken_email

def test_service_classification():
    assert classify_service("water is flooding my basement")["service_status"] == "supported"
    assert classify_service("I need roof repair")["service_status"] == "unsupported"
    assert classify_service("something unusual at the property")["service_status"] == "review"

def test_contact_normalizers():
    assert normalize_email("josh at example dot com") == "josh@example.com"
    assert normalize_email("No, it's josh at example dot com") == "josh@example.com"
    assert normalize_email("My email address is josh at example dot com") == "josh@example.com"
    assert normalize_phone("231 884 0943") == "+12318840943"
    assert normalize_confirmation("Yes, that's right") == "yes"
    assert normalize_confirmation("No, wrong") == "no"


def test_spelled_email_hyphens_are_treated_as_recognition_separators():
    assert normalize_email("j-o-s-h at example dot com") == "josh@example.com"
    assert normalize_email("j - o - s - h at example dot com") == "josh@example.com"
    assert normalize_email("J-O-A-C-H, S-H at gmail.com.") == "joachsh@gmail.com"
    assert normalize_email("J.O. aldrich at gmail dot com") == "joaldrich@gmail.com"


def test_real_email_hyphens_are_preserved():
    assert normalize_email("mary-jane at example dot com") == "mary-jane@example.com"
    assert normalize_email("mary dash jane at example dot com") == "mary-jane@example.com"
    assert normalize_email("mary hyphen jane at example dot com") == "mary-jane@example.com"


def test_email_readback_spells_letters_and_only_speaks_real_hyphens():
    assert spoken_email("j2@example.com") == "j, two, at, e, x, a, m, p, l, e, dot, c, o, m"
    assert "hyphen" not in spoken_email("josh@example.com")
    assert "hyphen" in spoken_email("mary-jane@example.com")
    assert "dash" not in spoken_email("mary-jane@example.com")


def test_address_readback_speaks_every_address_and_zip_digit_separately():
    spoken = spoken_address("8805 East Melendy Street, Ludington, MI 49431")
    assert spoken.startswith("eight, eight, zero, five, East Melendy Street")
    assert spoken.endswith("four, nine, four, three, one")
    assert not any(character.isdigit() for character in spoken)


def test_property_context_is_reused_from_volunteered_details():
    assert classify_property_context("I have mold in my house") == "Residential property"
    assert classify_property_context("This is for our commercial office") == "Commercial or managed property"
    assert classify_property_context("There is water coming in") == ""


def test_short_home_recognition_confusion_is_scoped_to_property_context():
    assert classify_property_context("hope") == "Residential property"


def test_name_prefix_is_removed_without_rewriting_the_callers_name():
    assert normalize_name("My name is Josh Aldrich.") == "Josh Aldrich"
    assert normalize_name("Uh, Josh Aldrich.") == "Josh Aldrich"
    assert normalize_name("Aldrich") == "Aldrich"


def test_partial_email_is_never_treated_as_complete():
    assert normalize_email("dot com") == ""


def test_emergency_detection_requires_active_water_or_specific_hazards():
    assert detect_emergency("Water is coming from a broken pipe and spreading into the hall")
    assert detect_emergency("There are sparks by the electrical panel")
    assert detect_emergency("Can you help? My pipe burst and the water is still flowing")
    assert not detect_emergency("The pipe broke, but the water is off and there are no electrical concerns")
    assert not detect_emergency("Do you offer emergency service?")
    assert not detect_emergency("Do you handle burst pipes?")
