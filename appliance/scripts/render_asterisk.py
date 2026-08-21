#!/usr/bin/env python3
from __future__ import annotations

import os
import textwrap
from pathlib import Path

from app.config import Settings


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def main() -> int:
    settings = Settings.from_env()
    data = settings.data_dir / "asterisk"
    etc = data / "etc"
    for path in (etc, data / "logs", data / "run", data / "spool", data / "db", data / "keys"):
        path.mkdir(parents=True, exist_ok=True)

    write(etc, "asterisk.conf", f"""
    [directories]
    astetcdir => {etc}
    astmoddir => /usr/lib/asterisk/modules
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
    write(etc, "logger.conf", """
    [general]
    dateformat=%F %T
    [logfiles]
    console => error
    full => notice,warning,error
    errors => error
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

    transport = settings.sip_transport if settings.sip_transport in {"udp", "tcp", "tls"} else "udp"
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
    contact = f"contact=sip:{settings.sip_server}:{settings.sip_port}" if settings.sip_server else ""
    matches = "\n".join(f"match={value}" for value in settings.sip_match_addresses)
    registration = ""
    if settings.sip_username and settings.sip_password and settings.sip_server:
        registration = f"""
        [floodman-registration]
        type=registration
        transport=transport-{transport}
        outbound_auth=floodman-trunk-auth
        server_uri=sip:{settings.sip_server}:{settings.sip_port}
        client_uri=sip:{settings.sip_username}@{settings.sip_server}
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
     same => n,Set(__FLOODMAN_CALL_ID=${{SHELL(cat /proc/sys/kernel/random/uuid)}})
     same => n,AGI(/opt/floodman/scripts/agi_prepare.py,${{FLOODMAN_CALL_ID}},${{CALLERID(num)}},${{FLOODMAN_DID}})
     same => n,AudioSocket(${{FLOODMAN_CALL_ID}},${{FLOODMAN_AUDIOSOCKET}})
     same => n,AGI(/opt/floodman/scripts/agi_finish.py,${{FLOODMAN_CALL_ID}})
     same => n,GotoIf($["${{FLOODMAN_ACTION}}"="transfer" & "${{FLOODMAN_TRANSFER_NUMBER}}"!=""]?human)
     same => n,Hangup()
     same => n(human),Set(CALLERID(name)=Floodman)
     same => n,Set(CALLERID(num)={caller_id})
     same => n,Dial(PJSIP/${{FLOODMAN_TRANSFER_NUMBER}}@floodman-trunk,45)
     same => n,Hangup()

    [floodman-outbound]
    exten => _X.,1,Set(CALLERID(name)=Floodman)
     same => n,Set(CALLERID(num)={caller_id})
     same => n,Dial(PJSIP/${{EXTEN}}@floodman-trunk,45)
     same => n,Hangup()
    exten => _+X.,1,Goto(floodman-outbound,${{EXTEN:1}},1)
    """)
    print(f"Rendered Asterisk configuration at {etc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
