#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from pathlib import Path

TWILIO_SIGNALING_CIDRS = (
    "54.172.60.0/30",      # us1 Virginia
    "54.244.51.0/30",      # us2 Oregon
    "54.171.127.192/30",   # ie1 Ireland
    "35.156.191.128/30",   # de1 Frankfurt
    "54.65.63.192/30",     # jp1 Tokyo
    "54.169.127.128/30",   # sg1 Singapore
    "54.252.254.64/30",    # au1 Sydney
    "177.71.206.192/30",   # br1 Sao Paulo
)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


def line(value: str, name: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"Unsafe newline in {name}")
    return value


def safe(value: str, pattern: str, name: str, allow_empty: bool = True) -> str:
    value = line(value, name)
    if not value and allow_empty:
        return value
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Unsafe {name}: {value!r}")
    return value


def safe_path(value: str, name: str, *, required: bool = False) -> str:
    value = line(value, name)
    if not value and not required:
        return ""
    if not value:
        raise ValueError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return str(path)


def write(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    path.chmod(mode)


def dial_line(extension: str, number: str, trunk: str) -> str:
    extension = safe(extension, r"[0-9A-Za-z_-]+", "extension", False)
    number = safe(number, r"\+[1-9][0-9]{7,14}", "transfer number") if number else ""
    if number:
        return (
            f"exten => {extension},1,NoOp(Floodman transfer {extension})\n"
            f" same => n,Dial(PJSIP/{number}@{trunk},35,b(floodman-transfer^s^1))\n"
            " same => n,Return()"
        )
    return (
        f"exten => {extension},1,NoOp(Floodman transfer destination not configured)\n"
        " same => n,Playback(sorry-youre-having-problems)\n"
        " same => n,Return()"
    )


def nat_lines(public_ip: str, local_net: str) -> str:
    external = ""
    if public_ip:
        external = (
            f"external_signaling_address={public_ip}\n"
            f"external_media_address={public_ip}\n"
        )
    return f"{external}local_net={local_net}\n"


def udp_transport(sip_port: int, public_ip: str, local_net: str) -> str:
    return f"""
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:{sip_port}
{nat_lines(public_ip, local_net).rstrip()}
allow_reload=yes
"""


def tls_transport(public_ip: str, local_net: str) -> str:
    bind_port = int(env("SIP_TLS_PORT", "5061"))
    cert_file = safe_path(env("SIP_TLS_CERT_FILE"), "SIP TLS certificate", required=True)
    key_file = safe_path(env("SIP_TLS_KEY_FILE"), "SIP TLS private key", required=True)
    ca_file = safe_path(
        env("SIP_TLS_CA_FILE", "/etc/ssl/certs/ca-certificates.crt"),
        "SIP TLS CA bundle",
        required=True,
    )
    verify_server = "yes" if env_bool("SIP_TLS_VERIFY_SERVER", True) else "no"
    return f"""
[transport-tls]
type=transport
protocol=tls
bind=0.0.0.0:{bind_port}
method=tlsv1_2
cert_file={cert_file}
priv_key_file={key_file}
ca_list_file={ca_file}
verify_server={verify_server}
verify_client=no
require_client_cert=no
allow_wildcard_certs=yes
tcp_keepalive_enable=yes
tcp_keepalive_idle_time=30
tcp_keepalive_interval_time=10
tcp_keepalive_probe_count=5
{nat_lines(public_ip, local_net).rstrip()}
allow_reload=no
"""


def twilio_pjsip(trunk: str, public_ip: str, local_net: str) -> str:
    termination_uri = safe(
        env("TWILIO_TERMINATION_URI"),
        r"[A-Za-z0-9.-]+\.pstn(?:\.[A-Za-z0-9-]+)?\.twilio\.com|[A-Za-z0-9.-]+\.pstn\.twilio\.com",
        "Twilio termination URI",
        False,
    ).lower()
    canonical_domain = re.sub(
        r"\.pstn\.[A-Za-z0-9-]+\.twilio\.com$",
        ".pstn.twilio.com",
        termination_uri,
    )
    username = safe(env("TWILIO_SIP_USERNAME"), r"[A-Za-z0-9_.@+-]{3,255}", "Twilio SIP username", False)
    password = safe(
        env("TWILIO_SIP_PASSWORD"),
        r"[A-Za-z0-9._-]{12,128}",
        "Twilio SIP password",
        False,
    )
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password) or not re.search(r"\d", password):
        raise ValueError("Twilio SIP password must include uppercase, lowercase, and a digit")
    secure = env_bool("TWILIO_SECURE_TRUNKING", False)
    transport_name = "transport-tls" if secure else "transport-udp"
    remote_port = int(env("TWILIO_REMOTE_SIP_PORT", "5061" if secure else "5060"))
    transport = tls_transport(public_ip, local_net) if secure else udp_transport(
        int(env("SIP_PORT", env("SIP_ALLOCATION", "5060"))), public_ip, local_net
    )
    codecs = safe(env("SIP_CODECS", "ulaw"), r"[A-Za-z0-9_,]+", "SIP codecs", False)
    from_number = safe(
        env("TWILIO_FROM_NUMBER", env("OUTBOUND_CALLER_ID_NUMBER", "")),
        r"\+[1-9][0-9]{7,14}",
        "Twilio From number",
    )
    contact_suffix = ";transport=tls" if secure else ""
    media_security = "\nmedia_encryption=sdes\nmedia_encryption_optimistic=no" if secure else ""
    rtp_symmetric = "yes" if env_bool("TWILIO_RTP_SYMMETRIC", False) else "no"
    from_user = f"\nfrom_user={from_number}" if from_number else ""
    cidrs = env_csv("TWILIO_SIGNALING_CIDRS", ",".join(TWILIO_SIGNALING_CIDRS))
    for cidr in cidrs:
        safe(cidr, r"[A-Fa-f0-9:.]+/[0-9]{1,3}", "Twilio signaling CIDR", False)
    matches = "\n".join(f"match={cidr}" for cidr in cidrs)

    return transport + f"""

[{trunk}]
type=endpoint
transport={transport_name}
context=from-floodman-trunk
disallow=all
allow={codecs}
direct_media=no
dtmf_mode=rfc4733
rtp_symmetric={rtp_symmetric}
force_rport=yes
rewrite_contact=yes
rtp_keepalive=20
rtp_timeout=90
timers=yes
aors={trunk}-aor
outbound_auth={trunk}-auth
from_domain={canonical_domain}
identify_by=ip
send_pai=yes
send_rpid=yes
trust_id_inbound=yes
trust_id_outbound=yes
{from_user.strip()}{media_security}

[{trunk}-auth]
type=auth
auth_type=userpass
username={username}
password={password}

[{trunk}-aor]
type=aor
contact=sip:{termination_uri}:{remote_port}{contact_suffix}
qualify_frequency=30
qualify_timeout=5.0
authenticate_qualify=no

[{trunk}-identify]
type=identify
endpoint={trunk}
{matches}
"""


def generic_pjsip(
    trunk: str,
    sip_mode: str,
    sip_port: int,
    public_ip: str,
    local_net: str,
) -> str:
    transport = udp_transport(sip_port, public_ip, local_net)
    if sip_mode == "disabled":
        return transport
    if sip_mode not in {"registration", "ip"}:
        raise ValueError("SIP_TRUNK_MODE must be disabled, registration, ip, or twilio")

    sip_server = safe(env("SIP_SERVER"), r"[A-Za-z0-9_.:\-\[\]]+", "SIP server", False)
    sip_username = line(env("SIP_USERNAME"), "SIP username")
    sip_password = line(env("SIP_PASSWORD"), "SIP password")
    if sip_mode == "registration" and (not sip_username or not sip_password):
        raise ValueError("SIP_USERNAME and SIP_PASSWORD are required for registration mode")
    sip_from_user = line(env("SIP_FROM_USER", sip_username), "SIP from user")
    sip_from_domain = safe(env("SIP_FROM_DOMAIN"), r"[A-Za-z0-9_.:-]+", "SIP from domain")
    sip_match = safe(
        env("SIP_MATCH", sip_server.split(":", 1)[0]),
        r"[A-Za-z0-9_.:/\-]+",
        "SIP match",
        False,
    )
    outbound_proxy = safe(
        env("SIP_OUTBOUND_PROXY"), r"[A-Za-z0-9_.:@;=\-]+", "outbound proxy"
    )
    codecs = safe(env("SIP_CODECS", "ulaw,alaw,g722"), r"[A-Za-z0-9_,]+", "SIP codecs", False)
    endpoint = f"""
[{trunk}]
type=endpoint
transport=transport-udp
context=from-floodman-trunk
disallow=all
allow={codecs}
direct_media=no
dtmf_mode=rfc4733
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
aors={trunk}-aor
"""
    if sip_mode == "ip":
        endpoint += "identify_by=ip\n"
    if sip_from_user:
        endpoint += f"from_user={sip_from_user}\n"
    if sip_from_domain:
        endpoint += f"from_domain={sip_from_domain}\n"
    if outbound_proxy:
        endpoint += f"outbound_proxy=sip:{outbound_proxy}\n"

    auth = ""
    if sip_username and sip_password:
        endpoint += f"outbound_auth={trunk}-auth\n"
        auth = f"""
[{trunk}-auth]
type=auth
auth_type=userpass
username={sip_username}
password={sip_password}
"""
    aor = f"""
[{trunk}-aor]
type=aor
contact=sip:{sip_server}
qualify_frequency=60
"""
    if sip_mode == "registration":
        route = f"""
[{trunk}-registration]
type=registration
transport=transport-udp
outbound_auth={trunk}-auth
server_uri=sip:{sip_server}
client_uri=sip:{sip_username}@{sip_server}
contact_user={sip_from_user}
retry_interval=60
forbidden_retry_interval=300
expiration=3600
line=yes
endpoint={trunk}
"""
    else:
        route = f"""
[{trunk}-identify]
type=identify
endpoint={trunk}
match={sip_match}
"""
    return transport + endpoint + auth + aor + route


def main() -> None:
    data_dir = Path(env("DATA_DIR", "/home/container/data"))
    destination = Path(env("ASTERISK_CONFIG_DIR", str(data_dir / "asterisk/etc")))
    runtime_root = data_dir / "asterisk"
    varlib = runtime_root / "varlib"
    spool = runtime_root / "spool"
    run = runtime_root / "run"
    logs = runtime_root / "logs"
    agi = runtime_root / "agi-bin"
    db = runtime_root / "db"
    keys = runtime_root / "keys"
    cache = runtime_root / "cache"
    cdr_custom = logs / "cdr-custom"
    for path in (
        destination,
        varlib,
        spool,
        run,
        logs,
        agi,
        db,
        keys,
        cache,
        cdr_custom,
    ):
        path.mkdir(parents=True, exist_ok=True)

    module_override = line(env("ASTERISK_MODULE_DIR"), "Asterisk module directory")
    if module_override:
        module_dir = Path(module_override)
    else:
        candidates = sorted(Path("/usr/lib").glob("*-linux-gnu/asterisk/modules"))
        candidates.extend(
            candidate
            for candidate in (Path("/usr/lib/asterisk/modules"),)
            if candidate not in candidates
        )
        if not candidates:
            raise RuntimeError("Could not locate the Asterisk module directory")
        module_dir = next((candidate for candidate in candidates if candidate.is_dir()), candidates[-1])
    if not module_dir.is_absolute():
        raise ValueError("ASTERISK_MODULE_DIR must be an absolute path")

    # Asterisk packaged data directory discovery. XML documentation and
    # built-in sounds live here and must remain readable even when runtime
    # state is redirected into DATA_DIR.
    asterisk_data_override = line(env("ASTERISK_DATA_DIR"), "Asterisk data directory")
    if asterisk_data_override:
        asterisk_data_dir = Path(asterisk_data_override)
    else:
        data_candidates = (
            Path("/usr/share/asterisk"),
            Path("/var/lib/asterisk"),
        )
        asterisk_data_dir = next(
            (
                candidate
                for candidate in data_candidates
                if (candidate / "documentation/core-en_US.xml").is_file()
            ),
            Path("/usr/share/asterisk"),
        )
    if not asterisk_data_dir.is_absolute():
        raise ValueError("ASTERISK_DATA_DIR must be an absolute path")
    documentation_file = asterisk_data_dir / "documentation/core-en_US.xml"
    if asterisk_data_override and not documentation_file.is_file():
        raise RuntimeError(
            "Could not locate Asterisk core XML documentation at "
            f"{documentation_file}. Set ASTERISK_DATA_DIR to the packaged "
            "Asterisk data directory."
        )

    trunk = safe(env("ASTERISK_TRUNK", "floodman-trunk"), r"[A-Za-z0-9_.-]+", "trunk", False)
    sip_port = int(env("SIP_PORT", env("SIP_ALLOCATION", "5060")))
    ari_port = int(env("ARI_PORT", "8088"))
    ami_port = int(env("AMI_PORT", "5038"))
    rtp_start = int(env("RTP_START", "10000"))
    rtp_end = int(env("RTP_END", "10040"))
    gate_port = int(env("GATE_PORT", "9019"))
    if rtp_end < rtp_start:
        raise ValueError("RTP_END must be greater than or equal to RTP_START")
    if rtp_end - rtp_start + 1 < 20:
        raise ValueError("The RTP range must contain at least 20 UDP ports")

    ari_user = safe(env("ARI_USERNAME", "floodman-ava"), r"[A-Za-z0-9_.-]+", "ARI user", False)
    ari_secret = line(env("ARI_SECRET", "change-me-ari"), "ARI secret")
    ami_user = safe(env("AMI_USERNAME", "floodman"), r"[A-Za-z0-9_.-]+", "AMI user", False)
    ami_secret = line(env("AMI_SECRET", "change-me-ami"), "AMI secret")
    public_ip = safe(env("PUBLIC_IP"), r"[A-Fa-f0-9:.]+", "public IP")
    local_net = safe(env("LOCAL_NET", "172.16.0.0/12"), r"[A-Fa-f0-9:./]+", "local network", False)
    sip_mode = env("SIP_TRUNK_MODE", "disabled").lower()

    write(
        destination / "asterisk.conf",
        f"""
[directories]
astcachedir => {cache}
astetcdir => {destination}
astmoddir => {module_dir}
astvarlibdir => {varlib}
astdbdir => {db}
astkeydir => {keys}
astdatadir => {asterisk_data_dir}
astagidir => {agi}
astspooldir => {spool}
astrundir => {run}
astlogdir => {logs}
astsbindir => /usr/sbin

[options]
defaultlanguage = en
documentation_language = en_US
live_dangerously = no
hideconnect = yes
lockconfdir = no
""",
    )
    write(destination / "modules.conf", """
[modules]
autoload=yes
noload=chan_sip.so
""")
    write(destination / "stasis.conf", """
[threadpool]
initial_size=5
idle_timeout_sec=20
max_size=50
""")
    write(destination / "logger.conf", """
[general]
dateformat=%F %T
queue_log=no

[logfiles]
console => notice,warning,error,verbose
full => notice,warning,error,verbose,debug
security => security
""")
    write(destination / "rtp.conf", f"""
[general]
rtpstart={rtp_start}
rtpend={rtp_end}
strictrtp=yes
probation=4
icesupport=no
""")
    write(destination / "http.conf", f"""
[general]
enabled=yes
bindaddr=127.0.0.1
bindport={ari_port}
prefix=
""")
    write(destination / "ari.conf", f"""
[general]
enabled = yes
pretty = no
allowed_origins = http://127.0.0.1

[{ari_user}]
type = user
read_only = no
password = {ari_secret}
""", 0o600)
    write(destination / "manager.conf", f"""
[general]
enabled = yes
port = {ami_port}
bindaddr = 127.0.0.1
displayconnects = no
timestampevents = yes

[{ami_user}]
secret = {ami_secret}
read = system,call
write = originate
permit = 127.0.0.1/255.255.255.255
""", 0o600)

    if sip_mode == "twilio":
        pjsip = twilio_pjsip(trunk, public_ip, local_net)
    else:
        pjsip = generic_pjsip(trunk, sip_mode, sip_port, public_ip, local_net)
    write(destination / "pjsip.conf", pjsip, 0o600)

    source_hint_lines = ""
    google_dids = [
        re.sub(r"[^0-9+]", "", item)
        for item in env("GOOGLE_DIDS").split(",")
        if item.strip()
    ]
    for did in google_dids:
        source_hint_lines += (
            f' same => n,ExecIf($["${{FLOODMAN_DID}}"="{did}"]?'
            "Set(__FLOODMAN_SOURCE_HINT=google_lsa))\n"
        )

    transfer_context = "\n\n".join(
        [
            dial_line(env("FLOODMAN_LIVE_EXTENSION", "6000"), env("FLOODMAN_LIVE_NUMBER"), trunk),
            dial_line(env("FLOODMAN_EMERGENCY_EXTENSION", "6001"), env("FLOODMAN_EMERGENCY_NUMBER"), trunk),
            dial_line(env("FLOODMAN_BILLING_EXTENSION", "6002"), env("FLOODMAN_BILLING_NUMBER"), trunk),
            dial_line(env("FLOODMAN_ESTIMATING_EXTENSION", "6003"), env("FLOODMAN_ESTIMATING_NUMBER"), trunk),
        ]
    )

    carrier_test_extensions = ""
    if env_bool("ENABLE_CARRIER_TEST_EXTENSIONS", False):
        carrier_test_extensions = f"""

[floodman-carrier-tests]
exten => 8990,1,NoOp(Twilio Play termination test one)
 same => n,Dial(PJSIP/+16504894546@{trunk},60)
 same => n,Hangup()
exten => 8991,1,NoOp(Twilio Play termination test two)
 same => n,Dial(PJSIP/+14154758378@{trunk},60)
 same => n,Hangup()
"""

    # ── Call recording configuration ──────────────────────────────────────────
    recording_enabled = env_bool("CALL_RECORDING_ENABLED", False)
    recording_dir = safe_path(
        env("CALL_RECORDING_STORAGE_DIR", str(data_dir / "recordings")),
        "CALL_RECORDING_STORAGE_DIR",
    ) or str(data_dir / "recordings")
    recording_fmt = re.sub(r"[^a-z0-9]", "", env("CALL_RECORDING_FORMAT", "wav").lower()) or "wav"
    recording_beep = env_bool("CALL_RECORDING_BEEP_ENABLED", False)
    recording_disclosure = env_bool("CALL_RECORDING_DISCLOSURE_ENABLED", True)

    inbound_test_mode = env("INBOUND_TEST_MODE", "off").strip().lower()
    if inbound_test_mode not in {"off", "playback", "echo"}:
        raise ValueError("INBOUND_TEST_MODE must be off, playback, or echo")
    inbound_test_lines = ""
    if inbound_test_mode == "playback":
        inbound_test_lines = (
            " same => n,Wait(1)\n"
            " same => n,Playback(demo-congrats)\n"
            " same => n,Hangup()\n"
        )
    elif inbound_test_mode == "echo":
        inbound_test_lines = (
            " same => n,Wait(1)\n"
            " same => n,Playback(demo-echotest)\n"
            " same => n,Echo()\n"
            " same => n,Hangup()\n"
        )


    # MixMonitor inbound lines — inserted after Answer(), before AGI gate_start.
    # Uses Asterisk UNIQUEID (digits and dots only) and FILTER to mask caller.
    # 'b' flag: record from bridge-start.  'B' flag: also capture pre-bridge.
    rec_inbound_lines = ""
    if recording_enabled:
        beep_flag = "bBr" if recording_beep else "bB"
        rec_inbound_lines = (
            f" same => n,Set(__FLOODMAN_REC_DIR={recording_dir})\n"
            f" same => n,Set(__FLOODMAN_REC_FILE=${{FLOODMAN_REC_DIR}}/${{EPOCH}}_${{UNIQUEID}}_inbound_${{FILTER(0-9+,${{CALLERID(num)}})}}.{recording_fmt})\n"
            f" same => n,Set(__FLOODMAN_RECORDING_ENABLED=1)\n"
            f" same => n,MixMonitor(${{FLOODMAN_REC_FILE}},{beep_flag})\n"
        )
        if recording_disclosure:
            # Play disclosure BEFORE AVA agent picks up; after gate classification.
            # The disclosure is a deterministic Playback — not an LLM decision.
            rec_inbound_lines += (
                " same => n,Set(__FLOODMAN_DISCLOSURE_PLAYED=0)\n"
            )

    # MixMonitor outbound lines — inserted at the start of the outbound context.
    rec_outbound_lines = ""
    if recording_enabled:
        beep_flag = "bBr" if recording_beep else "bB"
        rec_outbound_lines = (
            f" same => n,Set(__FLOODMAN_REC_DIR={recording_dir})\n"
            f" same => n,Set(__FLOODMAN_REC_FILE=${{FLOODMAN_REC_DIR}}/${{EPOCH}}_${{UNIQUEID}}_outbound_${{FILTER(0-9+,${{CALLERID(num)}})}}.{recording_fmt})\n"
            f" same => n,Set(__FLOODMAN_RECORDING_ENABLED=1)\n"
            f" same => n,MixMonitor(${{FLOODMAN_REC_FILE}},{beep_flag})\n"
        )

    extensions = f"""
[general]
static=yes
writeprotect=no
clearglobalvars=no

[globals]
FLOODMAN_GATE_SERVICE=127.0.0.1:{gate_port}

[default]
include => from-floodman-trunk
include => from-internal

[from-floodman-trunk]
exten => _.,1,Goto(floodman-inbound,s,1)
exten => s,1,Goto(floodman-inbound,s,1)

[floodman-inbound]
exten => s,1,NoOp(Floodman inbound call gate)
 same => n,Set(__FLOODMAN_DIRECTION=inbound)
 same => n,Set(__FLOODMAN_DID=${{CALLERID(dnid)}})
 same => n,ExecIf($["${{FLOODMAN_DID}}"=""]?Set(__FLOODMAN_DID=${{EXTEN}}))
 same => n,Set(__FLOODMAN_DIVERSION=${{PJSIP_HEADER(read,Diversion)}})
 same => n,Set(__FLOODMAN_DIVERSION_URI=${{CUT(FLOODMAN_DIVERSION,:,2)}})
 same => n,Set(__FLOODMAN_DIVERSION_USER=${{CUT(FLOODMAN_DIVERSION_URI,@,1)}})
 same => n,Set(__FLOODMAN_DIVERSION_DID=${{FILTER(0-9+,${{FLOODMAN_DIVERSION_USER}})}})
 same => n,ExecIf($["${{FLOODMAN_DIVERSION_DID}}"!=""]?Set(__FLOODMAN_DID=${{FLOODMAN_DIVERSION_DID}}))
 same => n,Set(__FLOODMAN_TRUNK=${{CHANNEL(pjsip,endpoint)}})
 same => n,Set(__FLOODMAN_SOURCE_HINT=)
{source_hint_lines.rstrip()}
 same => n,Answer()
{inbound_test_lines.rstrip()}
{rec_inbound_lines.rstrip()}
 same => n,AGI({agi / 'agi_gate_start.py'})
 same => n,GotoIf($["${{FLOODMAN_GATE_BYPASS}}"="1"]?ava)
 same => n,TryExec(AudioSocket(${{FLOODMAN_GATE_UUID}},${{FLOODMAN_GATE_SERVICE}}))
 same => n,NoOp(Floodman gate AudioSocket completed with ${{TRYSTATUS}})
 same => n,AGI({agi / 'agi_gate_finish.py'})
 same => n(ava),Set(__AI_AGENT=${{IF($["${{AI_AGENT}}"=""]?floodman_inbound:${{AI_AGENT}})}})
 same => n,Set(__AI_PROVIDER=${{IF($["${{AI_PROVIDER}}"=""]?{env('DEFAULT_PROVIDER','local_hybrid')}:${{AI_PROVIDER}})}})
 same => n,Stasis({safe(env('AVA_STASIS_APP','asterisk-ai-voice-agent'), r'[A-Za-z0-9_.-]+', 'Stasis app', False)})
 same => n,GotoIf($["${{STASISSTATUS}}"="FAILED"]?provider-failure)
 same => n,Hangup()
 same => n(provider-failure),Goto(floodman-provider-failure,s,1)
exten => h,1,NoOp(Floodman inbound hangup handler)
 same => n,AGI({agi / 'agi_record_finalize.py'})

[floodman-outbound]
exten => s,1,NoOp(Floodman outbound call)
 same => n,Set(__FLOODMAN_DIRECTION=outbound)
{rec_outbound_lines.rstrip()}
 same => n,Answer()
 same => n,Stasis({safe(env('AVA_STASIS_APP','asterisk-ai-voice-agent'), r'[A-Za-z0-9_.-]+', 'Stasis app', False)})
 same => n,Hangup()
exten => h,1,NoOp(Floodman outbound hangup handler)
 same => n,AGI({agi / 'agi_record_finalize.py'})

[floodman-transfer]
exten => s,1,NoOp(Floodman transfer pre-bridge)
 same => n,Return()

[from-internal]
{transfer_context}
{'include => floodman-carrier-tests' if carrier_test_extensions else ''}

[ext-queues]
include => from-internal

[ext-group]
include => from-internal

[floodman-provider-failure]
exten => s,1,NoOp(Floodman AVA provider failure fallback)
 same => n,Gosub(from-internal,{env('FLOODMAN_LIVE_EXTENSION','6000')},1)
 same => n,Hangup()

[floodman-test-call]
exten => s,1,NoOp(Floodman controlled two-way audio test)
 same => n,Answer()
 same => n,Wait(1)
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Hangup()
{carrier_test_extensions}
"""
    write(destination / "extensions.conf", extensions)
    write(destination / "voicemail.conf", """
[general]
format=wav49|gsm|wav
attach=no
maxsecs=180
minsecs=2
""")
    write(destination / "cdr.conf", "[general]\nenable=yes\nunanswered=yes\ncongestion=yes")
    write(
        destination / "cdr_custom.conf",
        "[mappings]\nMaster.csv => ${CSV_QUOTE(${CDR(clid)})},${CSV_QUOTE(${CDR(src)})},${CSV_QUOTE(${CDR(dst)})},${CSV_QUOTE(${CDR(start)})},${CSV_QUOTE(${CDR(answer)})},${CSV_QUOTE(${CDR(end)})},${CSV_QUOTE(${CDR(duration)})},${CSV_QUOTE(${CDR(billsec)})},${CSV_QUOTE(${CDR(disposition)})},${CSV_QUOTE(${CDR(uniqueid)})}",
    )
    print(f"Rendered embedded Asterisk configuration into {destination} (SIP mode: {sip_mode})")


if __name__ == "__main__":
    main()
