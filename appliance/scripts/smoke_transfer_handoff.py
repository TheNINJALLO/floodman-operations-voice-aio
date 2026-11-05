#!/usr/bin/env python3
"""Exercise the real Asterisk media -> AGI -> Dial handoff without a carrier.

Run inside the built CPU image with --network none and DATA_DIR on a writable
tmpfs. The staff destination is a Local channel; no PSTN call or AI inference.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = Path(os.environ.get("DATA_DIR", "/home/container/data"))
DATA.mkdir(parents=True, exist_ok=True)


async def run() -> None:
    executable = shutil.which("asterisk")
    if not executable:
        raise RuntimeError("Asterisk is required for the handoff smoke test")
    with tempfile.TemporaryDirectory(prefix="handoff-smoke-", dir=DATA) as temporary:
        work = Path(temporary)
        os.environ.update(DATA_DIR=str(work), RUNTIME_DIR=str(work / "runtime"), SIP_MODE="disabled",
                          PUBLIC_BASE_URL="http://127.0.0.1", TMPDIR=str(work / "tmp"),
                          LOCAL_AI_ONLY="true", PYTHONDONTWRITEBYTECODE="1")
        (work / "tmp").mkdir()
        from app.audiosocket import AudioSocketConnection, TYPE_AUDIO
        from app.config import Settings
        from app.registry import CallRegistry
        from render_asterisk import main as render

        registry = CallRegistry(work / "runtime")
        handled = asyncio.Event()
        errors: list[str] = []

        async def voice_stub(reader, writer):
            try:
                connection = AudioSocketConnection(reader, writer, Settings.from_env())
                call_uuid = await connection.start()
                # Enter the real media receive path before ending the AI leg.
                writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", 320) + b"\x00" * 320)
                await writer.drain()
                await asyncio.sleep(.05)
                registry.write_action(call_uuid, "transfer", "+12318840943", "assistant_transfer")
                await connection.close()
                handled.set()
            except Exception as exc:
                errors.append(type(exc).__name__)
                handled.set()
                writer.close()

        server = await asyncio.start_server(voice_stub, "127.0.0.1", 0)
        os.environ["AUDIOSOCKET_PORT"] = str(server.sockets[0].getsockname()[1])
        os.environ["AUDIOSOCKET_HOST"] = "127.0.0.1"
        assert render() == 0
        etc = work / "asterisk" / "etc"
        extensions = (etc / "extensions.conf").read_text()
        production_dial = "Dial(PJSIP/${FLOODMAN_TRANSFER_NUMBER}@floodman-trunk,45,"
        assert extensions.count(production_dial) == 1
        # Replace only the carrier leg, retaining actual production AGI, routing,
        # ringback and the answered-transfer callback in the generated dialplan.
        extensions = extensions.replace(production_dial,
            "Log(NOTICE,SMOKE_NUMBER=${FLOODMAN_TRANSFER_NUMBER})\n same => n,Dial(Local/staff@smoke-staff/n,3,")
        extensions += """
[smoke-entry]
exten => start,1,Goto(floodman-inbound,s,1)
[smoke-staff]
exten => staff,1,Ringing()
 same => n,Wait(0.1)
 same => n,Answer()
 same => n,Wait(0.1)
 same => n,Hangup()
"""
        (etc / "extensions.conf").write_text(extensions)
        config = etc / "asterisk.conf"
        output_path = work / "asterisk-process.log"
        started = time.monotonic()
        with output_path.open("w") as output:
            process = subprocess.Popen([executable, "-f", "-n", "-C", str(config)], stdout=output, stderr=subprocess.STDOUT)
            try:
                control = work / "asterisk" / "run" / "asterisk.ctl"
                for _ in range(100):
                    if control.exists():
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Isolated Asterisk exited during startup")
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError("Isolated Asterisk control socket was unavailable")
                # Control socket can precede module/dialplan loading.
                for _ in range(100):
                    result = await asyncio.to_thread(subprocess.run,
                        [executable, "-C", str(config), "-rx", "dialplan show smoke-entry"],
                        capture_output=True, text=True, timeout=3)
                    if "start" in result.stdout and "smoke-entry" in result.stdout and "does not exist" not in result.stdout:
                        break
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError("Isolated Asterisk dialplan did not load")
                result = await asyncio.to_thread(subprocess.run,
                    [executable, "-C", str(config), "-rx", "channel originate Local/start@smoke-entry application Wait 5"],
                    capture_output=True, text=True, timeout=5)
                if result.returncode:
                    raise RuntimeError("Could not originate isolated Local channel")
                await asyncio.wait_for(handled.wait(), timeout=6)
                if errors:
                    raise RuntimeError("Voice stub failed: " + ",".join(errors))
                log_path = work / "logs" / "asterisk-full.log"
                for _ in range(100):
                    logs = log_path.read_text(errors="replace") if log_path.exists() else ""
                    if "stage=transfer_connected" in logs:
                        break
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError("AudioSocket did not hand control back through AGI to Dial")
                assert "stage=audiosocket_return" in logs
                assert "action=transfer reason=assistant_transfer" in logs
                assert "SMOKE_NUMBER=+12318840943" in logs
                assert not list((work / "runtime" / "actions").glob("*.json"))
                print(json.dumps({"asterisk_transfer_handoff": "passed", "carrier_calls": 0,
                                  "agi_action_consumed": True, "staff_leg_answered": True,
                                  "duration_seconds": round(time.monotonic() - started, 2)}), flush=True)
            except Exception:
                # This container contains synthetic identifiers and no secrets.
                for path in (output_path, work / "logs" / "asterisk-full.log"):
                    if path.exists():
                        print(path.read_text(errors="replace")[-16000:], file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=3)
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(run())
