#!/usr/bin/env python3
"""Run the pinned 8B model on loopback, then verify bounded wording fallback."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.tel_agent_runtime import TelAgentLLM
from app.tel_agent_phone import TelAgentPhoneAgent
from app.models import IntakeState, VoiceReply
from app.model_profile import select_primary_model
from app.trial_wording import valid_question_rewrite
from download_models import configure_runtime_cache, verify_primary_model


async def verify(settings):
    model = TelAgentLLM(settings)
    cases = [
        ("water_condition", "Water in my basement", "Is water still coming in or rising, or has it stopped?"),
        ("affected_area", "I need help with a leak", "Which area is affected?"),
        ("timing_summary", "The basement is damp", "When did you first notice this?"),
    ]
    results = []
    for stage, caller, required in cases:
        started = time.perf_counter()
        first = None
        chunks = []
        async for text in model.stream_text([
            {"role": "system", "content": "You are Alex, Floodman's phone assistant. Rephrase only the supplied question naturally in one short sentence. Ask exactly one question. Do not add facts, actions, promises or other questions. Output only the question."},
            {"role": "user", "content": f"Caller said: {caller}\nQuestion to rephrase: {required}"},
        ], max_tokens=60, temperature=0):
            if first is None:
                first = (time.perf_counter() - started) * 1000
            chunks.append(text)
        candidate = "".join(chunks).strip()
        valid = valid_question_rewrite(candidate, required, stage)
        complete_ms = (time.perf_counter() - started) * 1000
        results.append({"stage": stage, "generated_text": candidate, "first_text_ms": round(first or 0, 2),
                        "complete_ms": round(complete_ms, 2), "wording_valid": valid,
                        "within_live_wording_budget": complete_ms <= 1200,
                        "approved_fallback": required})
        if not candidate:
            raise RuntimeError("Pinned local model returned no text")
    required = cases[0][2]
    core = SimpleNamespace(settings=settings, database=SimpleNamespace(get_call=lambda _id: {"messages": []}))
    phone = TelAgentPhoneAgent(core, None, model, None, None)
    call = SimpleNamespace(call_id=1, state=IntakeState(call_uuid="synthetic-local-model", stage="water_condition"))
    started = time.perf_counter()
    bounded = "".join([part async for part in phone._reply_stream(call, cases[0][1], VoiceReply(text=required, speech_parts=(required,)), "issue")])
    bounded_ms = (time.perf_counter() - started) * 1000
    if bounded_ms > 1800 or not valid_question_rewrite(bounded, required, "water_condition"):
        raise RuntimeError("Local wording failed the bounded-response acceptance check")
    protected = VoiceReply(text="I'm connecting you with Floodman's emergency contact now.", transfer_number="+12315550101")
    exact = "".join([part async for part in phone._reply_stream(call, "Water is rising", protected, "issue")])
    if exact != protected.text:
        raise RuntimeError("The model changed a protected handoff")
    report = {"local_llm_trial": "passed", "model": settings.llama_model_alias,
              "weights_checksum_verified": True, "cpu_only": True,
              "carrier_calls": 0, "cloud_ai_requests": 0, "cases": results,
              "bounded_wording_ms": round(bounded_ms, 2), "bounded_wording_text": bounded,
              "protected_handoff_preserved": True,
              "note": "Synthetic CPU check. Out-of-budget or rejected wording uses the prepared response. This does not establish Ryzen carrier latency."}
    target = settings.data_dir / "benchmark/llm-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


def main():
    data = Path(os.environ["DATA_DIR"]).resolve()
    configure_runtime_cache(data)
    os.environ.update(PRIMARY_MODEL_PROFILE="haiku8b", LOCAL_AI_ONLY="true", LOCAL_AI_DEVICE="cpu",
                      LLAMA_BASE_URL="http://127.0.0.1:8181/v1", LLAMA_TIMEOUT_SECONDS="30")
    settings = Settings.from_env()
    primary = select_primary_model(settings.model_dir)
    verify_primary_model(primary.path, primary)
    binary = Path(os.getenv("LLAMA_RUNTIME_DIR", "/opt/llama")) / ("llama-server.exe" if os.name == "nt" else "llama-server")
    log = data / "logs/llm-trial-smoke.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        process = subprocess.Popen([
            str(binary), "--model", str(primary.path), "--alias", primary.alias,
            "--host", "127.0.0.1", "--port", "8181", "--n-gpu-layers", "0",
            "--ctx-size", "2048", "--threads", "4", "--threads-batch", "4", "--parallel", "1", "--jinja",
            "--batch-size", "128", "--ubatch-size", "64",
            "--api-key", settings.llama_api_key,
        ], stdout=handle, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            with httpx.Client(timeout=3, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Local model process exited before readiness")
                    try:
                        if client.get("http://127.0.0.1:8181/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("Local model startup exceeded the smoke-test deadline")
            asyncio.run(verify(settings))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
