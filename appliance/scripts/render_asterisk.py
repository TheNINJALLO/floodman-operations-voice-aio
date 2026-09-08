#!/usr/bin/env python3
from __future__ import annotations

import os
import textwrap
from pathlib import Path

from app.config import Settings, parse_sip_target


def detect_asterisk_module_dir() -> Path:
    configured = os.getenv("ASTERISK_MODULE_DIR", "").strip()
    candidates: list[Path] = []

    if configured:
        candidates.append(Path(configured))

    candidates.extend(
        (
            Path("/usr/lib/x86_64-linux-gnu/asterisk/modules"),
            Path("/usr/lib64/asterisk/modules"),
            Path("/usr/lib/asterisk/modules"),
        )
    )

    for base in (Path("/usr/lib"), Path("/usr/lib64")):
        if base.is_dir():
            candidates.extend(
                sorted(base.glob("*/asterisk/modules"))
            )

    checked: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        checked.append(normalized)

        if (
            candidate.is_dir()
            and (candidate / "app_audiosocket.so").is_file()
        ):
            return candidate

    raise RuntimeError(
        "Asterisk AudioSocket module directory was not found. "
        "Checked: " + ", ".join(checked)
    )


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def main() -> int:
    settings = Settings.from_env()
    module_dir = detect_asterisk_module_dir()
    data = settings.data_dir / "asterisk"
    etc = data / "etc"
    for path in (etc, data / "logs", data / "run", data / "spool", data / "db", data / "keys"):
        path.mkdir(parents=True, exist_ok=True)

    write(etc, "asterisk.conf", f"""
    [directories]
    astetcdir => {etc}
    astmoddir => {module_dir}
    astvarlibdir => /usr/share/asterisk
    astdbdir => {data / 'db'}
    astkeydir => {data / 'keys'}
    astdatadir => /usr/share/asterisk
    astagidir => /opt/floodman/scripts
    astspooldir => {data / 'spool'}
    astrundir => {data / 'run'}
    astlogdir => {data / 'logs'}
    [options]
    verbose = 0
    debug = 0
    documentation_language = en_US
    """)
    write(etc, "logger.conf", f"""
    [general]
    dateformat=%F %T
    rotatestrategy=rotate
    [logfiles]
    console => notice,warning,error
    {settings.log_dir / 'asterisk-full.log'} => notice,warning,error
    {settings.log_dir / 'asterisk-errors.log'} => error
    """)
    write(etc, "modules.conf", """
    [modules]
    autoload=yes
    noload => res_config_odbc.so
    noload => res_odbc.so
    noload => cdr_odbc.so
    noload => cel_odbc.so
    noload => func_odbc.so
    noload => res_config_pgsql.so
    noload => cdr_pgsql.so
    noload => cel_pgsql.so
    noload => res_config_ldap.so
    noload => res_ldap.so
    noload => res_xmpp.so
    noload => chan_motif.so
    noload => chan_iax2.so
    noload => chan_unistim.so
    noload => chan_console.so
    noload => app_confbridge.so
    noload => app_page.so
    noload => res_parking.so
    noload => app_agent_pool.so
    noload => res_calendar.so
    noload => res_prometheus.so
    noload => app_voicemail_odbc.so
    noload => app_voicemail_imap.so
    noload => pbx_lua.so
    noload => pbx_ael.so
    noload => res_phoneprov.so
    noload => app_queue.so
    noload => app_festival.so
    noload => res_snmp.so
    """)
    write(etc, "rtp.conf", """
    [general]
    rtpstart=10000
    rtpend=10100
    strictrtp=yes
    """)
    write(etc, "http.conf", "[general]\nenabled=no")
    write(etc, "manager.conf", "[general]\nenabled=no")
    write(etc, "indications.conf", "[general]\ncountry=us\n[us]\ndescription=United States\nringcadence=2000,4000")

    sip_target = parse_sip_target(settings.sip_server, settings.sip_port) if settings.sip_server else None
    transport = (sip_target.transport if sip_target else None) or settings.sip_transport
    transport = transport if transport in {"udp", "tcp", "tls"} else "udp"
    protocol = "tls" if transport == "tls" else transport
    bind_port = settings.sip_port
    external = f"external_signaling_address={settings.sip_public_ip}\nexternal_media_address={settings.sip_public_ip}" if settings.sip_public_ip else ""
    auth = ""
    endpoint_auth = ""
    if settings.sip_username and settings.sip_password:
        auth = f"""
        [floodman-trunk-auth]
        type=auth
        auth_type=userpass
        username={settings.sip_username}
        password={settings.sip_password}
        """
        endpoint_auth = "outbound_auth=floodman-trunk-auth\nauth=floodman-trunk-auth"
    proxy = f"outbound_proxy={settings.sip_outbound_proxy}" if settings.sip_outbound_proxy else ""
    from_user = f"from_user={settings.sip_from_user}" if settings.sip_from_user else ""
    from_domain = f"from_domain={settings.sip_from_domain}" if settings.sip_from_domain else ""
    contact = f"contact={sip_target.contact_uri}" if sip_target else ""
    matches = "\n".join(f"match={value}" for value in settings.sip_match_addresses)
    registration = ""
    if settings.sip_username and settings.sip_password and sip_target:
        registration = f"""
        [floodman-registration]
        type=registration
        transport=transport-{transport}
        outbound_auth=floodman-trunk-auth
        server_uri={sip_target.contact_uri}
        client_uri=sip:{settings.sip_username}@{sip_target.host_literal}
        retry_interval=30
        forbidden_retry_interval=300
        expiration=3600
        {proxy}
        """
    write(etc, "pjsip.conf", f"""
    [global]
    type=global
    user_agent=Floodman Voice Appliance

    [transport-{transport}]
    type=transport
    protocol={protocol}
    bind=0.0.0.0:{bind_port}
    local_net={settings.sip_local_net}
    {external}

    {auth}

    [floodman-trunk-aor]
    type=aor
    {contact}
    qualify_frequency=60

    [floodman-trunk]
    type=endpoint
    transport=transport-{transport}
    context=from-floodman-trunk
    disallow=all
    allow=ulaw,alaw
    aors=floodman-trunk-aor
    direct_media=no
    rtp_symmetric=yes
    force_rport=yes
    rewrite_contact=yes
    dtmf_mode=rfc4733
    {endpoint_auth}
    {proxy}
    {from_user}
    {from_domain}

    [floodman-identify]
    type=identify
    endpoint=floodman-trunk
    {matches}

    {registration}
    """)
    caller_id = settings.outbound_caller_id_number or settings.twilio_phone_number
    write(etc, "extensions.conf", f"""
    [globals]
    FLOODMAN_AUDIOSOCKET={settings.audiosocket_host}:{settings.audiosocket_port}

    [from-floodman-trunk]
    exten => s,1,Set(__FLOODMAN_DID={settings.twilio_phone_number})
     same => n,Goto(floodman-inbound,s,1)
    exten => _X.,1,Set(__FLOODMAN_DID=${{EXTEN}})
     same => n,Goto(floodman-inbound,s,1)
    exten => _+X.,1,Set(__FLOODMAN_DID=${{EXTEN}})
     same => n,Goto(floodman-inbound,s,1)

    [floodman-inbound]
    exten => s,1,NoOp(Floodman Voice Appliance inbound call)
     same => n,Answer()
     same => n,Set(TIMEOUT(absolute)=1800)
     same => n,Set(__FLOODMAN_CALL_ID=${{SHELL(head -c 36 /proc/sys/kernel/random/uuid)}})
     same => n,Set(__FLOODMAN_CHANNEL_ID=${{CHANNEL(uniqueid)}})
     same => n,Set(__FLOODMAN_SIP_CALL_ID=${{PJSIP_HEADER(read,Call-ID)}})
     same => n,Log(NOTICE,FLOODMAN_CALL stage=answered call_uuid=${{FLOODMAN_CALL_ID}} channel_id=${{FLOODMAN_CHANNEL_ID}} sip_call_id=${{FLOODMAN_SIP_CALL_ID}} caller=${{CALLERID(num)}} called=${{FLOODMAN_DID}})
     same => n,AGI(/opt/floodman/scripts/agi_prepare.py,${{FLOODMAN_CALL_ID}},${{CALLERID(num)}},${{FLOODMAN_DID}},${{FLOODMAN_CHANNEL_ID}},${{FLOODMAN_SIP_CALL_ID}})
     same => n,GotoIf($["${{FLOODMAN_PREPARED}}"="1"]?audio:fallback)
     same => n(audio),TryExec(AudioSocket(${{FLOODMAN_CALL_ID}},${{FLOODMAN_AUDIOSOCKET}}))
     same => n,Set(__FLOODMAN_AUDIO_TRYSTATUS=${{TRYSTATUS}})
     same => n,Log(NOTICE,FLOODMAN_CALL stage=audiosocket_return call_uuid=${{FLOODMAN_CALL_ID}} channel_id=${{FLOODMAN_CHANNEL_ID}} trystatus=${{FLOODMAN_AUDIO_TRYSTATUS}})
     same => n,GotoIf($["${{FLOODMAN_AUDIO_TRYSTATUS}}"="SUCCESS"]?finish:fallback)
     same => n(finish),Set(FLOODMAN_ACTION=missing_action)
     same => n,AGI(/opt/floodman/scripts/agi_finish.py,${{FLOODMAN_CALL_ID}})
     same => n,Log(NOTICE,FLOODMAN_CALL stage=finish call_uuid=${{FLOODMAN_CALL_ID}} action=${{FLOODMAN_ACTION}} reason=${{FLOODMAN_ACTION_REASON}})
     same => n,GotoIf($["${{FLOODMAN_ACTION}}"="transfer" & "${{FLOODMAN_TRANSFER_NUMBER}}"!=""]?human)
     same => n,GotoIf($["${{FLOODMAN_ACTION}}"="completed" | "${{FLOODMAN_ACTION}}"="fallback_callback" | "${{FLOODMAN_ACTION}}"="caller_hangup"]?normal-end)
     same => n,Goto(fallback)
     same => n(fallback),Log(ERROR,FLOODMAN_CALL stage=technical_fallback call_uuid=${{FLOODMAN_CALL_ID}} channel_id=${{FLOODMAN_CHANNEL_ID}} trystatus=${{FLOODMAN_AUDIO_TRYSTATUS}} action=${{FLOODMAN_ACTION}} reason=${{FLOODMAN_ACTION_REASON}})
     same => n,Playback(floodman-technical-failure)
     same => n,Hangup()
     same => n(normal-end),Hangup()
     same => n(human),Set(CALLERID(name)=Floodman)
     same => n,Set(CALLERID(num)={caller_id})
     same => n,Dial(PJSIP/${{FLOODMAN_TRANSFER_NUMBER}}@floodman-trunk,45)
     same => n,Hangup()
    exten => h,1,Log(NOTICE,FLOODMAN_CALL stage=hangup call_uuid=${{FLOODMAN_CALL_ID}} channel_id=${{FLOODMAN_CHANNEL_ID}} sip_call_id=${{FLOODMAN_SIP_CALL_ID}} cause=${{HANGUPCAUSE}} source=${{CHANNEL(hangupsource)}})

    [floodman-outbound]
    exten => _X.,1,Set(CALLERID(name)=Floodman)
     same => n,Set(CALLERID(num)={caller_id})
     same => n,Dial(PJSIP/${{EXTEN}}@floodman-trunk,45)
     same => n,Hangup()
    exten => _+X.,1,Goto(floodman-outbound,${{EXTEN:1}},1)
    """)
    print(
        f"Rendered Asterisk configuration at {etc} "
        f"(modules: {module_dir}; SIP target: {sip_target.contact_uri if sip_target else 'disabled'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
