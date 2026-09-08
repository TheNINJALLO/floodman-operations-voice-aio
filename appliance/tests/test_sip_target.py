import pytest

from app.config import parse_sip_target


@pytest.mark.parametrize(
    ("value", "default_port", "authority", "transport"),
    [
        ("carrier.example.com", 5060, "carrier.example.com:5060", None),
        ("carrier.example.com:5061", 5060, "carrier.example.com:5061", None),
        ("sip:carrier.example.com", 5060, "carrier.example.com:5060", None),
        ("sip:carrier.example.com:5060", 5061, "carrier.example.com:5060", None),
        ("sip:carrier.example.com:5061;transport=tls", 5060, "carrier.example.com:5061", "tls"),
        ("sips:carrier.example.com:5061", 5060, "carrier.example.com:5061", "tls"),
        ("192.0.2.10", 5060, "192.0.2.10:5060", None),
        ("[2001:db8::10]:5061", 5060, "[2001:db8::10]:5061", None),
        ("2001:db8::10", 5060, "[2001:db8::10]:5060", None),
    ],
)
def test_parse_sip_target(value: str, default_port: int, authority: str, transport: str | None) -> None:
    target = parse_sip_target(value, default_port)
    assert target.authority == authority
    assert target.transport == transport
    assert target.contact_uri == f"sip:{authority}" + (f";transport={transport}" if transport else "")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "carrier.example.com:5060:5060",
        "sip://carrier.example.com",
        "https://carrier.example.com",
        "sip:user@carrier.example.com",
        "sip:carrier.example.com/path",
        "sip:carrier.example.com;transport=ws",
        "sips:carrier.example.com;transport=udp",
        "carrier.example.com:not-a-port",
        "[2001:db8::10",
    ],
)
def test_parse_sip_target_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_sip_target(value)
