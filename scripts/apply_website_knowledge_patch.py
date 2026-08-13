#!/usr/bin/env python3
"""Apply the Floodman.com knowledge-library wiring to an existing repository checkout."""
from __future__ import annotations

import argparse
from pathlib import Path


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def patch_config(root: Path) -> None:
    path = root / "app/config.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "    web_dir: Path\n\n    app_name: str = \"Floodman Operations Voice AIO\"",
        "    web_dir: Path\n    knowledge_dir: Path\n\n    app_name: str = \"Floodman Operations Voice AIO\"",
        "config knowledge_dir field",
    )
    text = replace_once(
        text,
        "    max_upload_bytes: int = 25 * 1024 * 1024\n\n    config: dict[str, Any] = field(default_factory=dict)",
        "    max_upload_bytes: int = 25 * 1024 * 1024\n\n"
        "    knowledge_require_approved: bool = True\n"
        "    knowledge_top_k: int = 4\n"
        "    knowledge_max_chars: int = 5200\n"
        "    knowledge_min_score: float = 0.8\n\n"
        "    config: dict[str, Any] = field(default_factory=dict)",
        "config knowledge settings fields",
    )
    text = replace_once(
        text,
        "        web_dir = Path(os.getenv(\"WEB_DIR\", root / \"web\"))\n        config = _load_yaml(config_dir / \"floodman.yaml\")",
        "        web_dir = Path(os.getenv(\"WEB_DIR\", root / \"web\"))\n"
        "        knowledge_dir = Path(os.getenv(\"KNOWLEDGE_DIR\", data_dir / \"knowledge\"))\n"
        "        if not knowledge_dir.is_absolute():\n"
        "            knowledge_dir = data_dir / knowledge_dir\n"
        "        config = _load_yaml(config_dir / \"floodman.yaml\")",
        "config knowledge path",
    )
    text = replace_once(
        text,
        "            web_dir=web_dir,\n            app_name=os.getenv(\"APP_NAME\", \"Floodman Operations Voice AIO\"),",
        "            web_dir=web_dir,\n"
        "            knowledge_dir=knowledge_dir,\n"
        "            app_name=os.getenv(\"APP_NAME\", \"Floodman Operations Voice AIO\"),",
        "config constructor knowledge path",
    )
    text = replace_once(
        text,
        "            max_upload_bytes=_env_int(\"MAX_UPLOAD_BYTES\", 25 * 1024 * 1024),\n            config=config,",
        "            max_upload_bytes=_env_int(\"MAX_UPLOAD_BYTES\", 25 * 1024 * 1024),\n"
        "            knowledge_require_approved=_env_bool(\"KNOWLEDGE_REQUIRE_APPROVED\", True),\n"
        "            knowledge_top_k=max(1, min(8, _env_int(\"KNOWLEDGE_TOP_K\", 4))),\n"
        "            knowledge_max_chars=max(800, min(12000, _env_int(\"KNOWLEDGE_MAX_CHARS\", 5200))),\n"
        "            knowledge_min_score=max(0.0, _env_float(\"KNOWLEDGE_MIN_SCORE\", 0.8)),\n"
        "            config=config,",
        "config constructor knowledge settings",
    )
    text = replace_once(
        text,
        "        self.call_recording_storage_dir.mkdir(parents=True, exist_ok=True)\n        for name in (\"gate-audio\", \"logs\", \"uploads\", \"recordings\", \"asterisk\"):",
        "        self.call_recording_storage_dir.mkdir(parents=True, exist_ok=True)\n"
        "        self.knowledge_dir.mkdir(parents=True, exist_ok=True)\n"
        "        for name in (\"gate-audio\", \"logs\", \"uploads\", \"recordings\", \"asterisk\"):",
        "config ensure knowledge directory",
    )
    write(path, text)


def patch_main(root: Path) -> None:
    path = root / "app/main.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from app.diagnostics import collect_diagnostics\n",
        "from app.diagnostics import collect_diagnostics\nfrom app.knowledge import KnowledgeBase, resolve_service_area\n",
        "main knowledge import",
    )
    text = replace_once(
        text,
        "        app.state.classifier = CallGateClassifier(settings)\n        app.state.gate_server = None",
        "        app.state.classifier = CallGateClassifier(settings)\n"
        "        app.state.knowledge = KnowledgeBase(\n"
        "            settings.knowledge_dir,\n"
        "            require_approved=settings.knowledge_require_approved,\n"
        "            default_top_k=settings.knowledge_top_k,\n"
        "            max_context_chars=settings.knowledge_max_chars,\n"
        "            min_score=settings.knowledge_min_score,\n"
        "        )\n"
        "        app.state.gate_server = None",
        "main knowledge state",
    )
    text = replace_once(
        text,
        '            "timezone": settings.timezone,\n            "agents": [',
        '            "timezone": settings.timezone,\n'
        '            "knowledge": request.app.state.knowledge.status(),\n'
        '            "agents": [',
        "main system knowledge status",
    )
    old_public_info = """    @app.post("/internal/tools/public-business-information", dependencies=[Depends(require_internal)])
    async def tool_business_information() -> dict[str, Any]:
        return {"ok": True, "business": settings.service_information}
"""
    new_public_info = """    @app.post("/internal/tools/public-business-information", dependencies=[Depends(require_internal)])
    async def tool_business_information(
        request: Request, payload: RoomflowToolRequest
    ) -> dict[str, Any]:
        business = settings.service_information
        services = business.get("services", {}) if isinstance(business, dict) else {}
        question = str(payload.data.get("question") or "").strip()
        knowledge = (
            request.app.state.knowledge.search(question, top_k=settings.knowledge_top_k)
            if question
            else {"found": False, "results": [], "answer_context": ""}
        )
        return {
            "ok": True,
            "business": {
                "public_name": business.get("public_name", "Floodman") if isinstance(business, dict) else "Floodman",
                "website": business.get("website", "https://floodman.com") if isinstance(business, dict) else "https://floodman.com",
                "primary_phone": business.get("primary_phone", "231-935-4921") if isinstance(business, dict) else "231-935-4921",
                "emergency_availability": business.get("emergency_availability", "") if isinstance(business, dict) else "",
                "services": [
                    str(value.get("public_name") or key)
                    for key, value in services.items()
                    if isinstance(value, dict) and value.get("website_advertises", True)
                ],
                "inspection_policy": business.get("inspection_policy", {}) if isinstance(business, dict) else {},
                "pricing_policy": business.get("pricing_policy", {}) if isinstance(business, dict) else {},
            },
            "knowledge": knowledge,
        }
"""
    if new_public_info not in text:
        if old_public_info not in text:
            raise PatchError("main public-business endpoint did not match expected source")
        text = text.replace(old_public_info, new_public_info, 1)
    admin_block = '''    @app.get("/api/v1/knowledge/status", dependencies=[Depends(require_admin)])
    async def knowledge_status(request: Request) -> dict[str, Any]:
        return request.app.state.knowledge.status()

    @app.post("/api/v1/knowledge/search", dependencies=[Depends(require_admin)])
    async def knowledge_search(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload.get("question") or "").strip()
        category = str(payload.get("category") or "").strip()
        try:
            top_k = int(payload.get("top_k") or settings.knowledge_top_k)
        except (TypeError, ValueError):
            top_k = settings.knowledge_top_k
        return request.app.state.knowledge.search(question, category=category, top_k=top_k)

'''
    marker = "    # Call gate ---------------------------------------------------------\n"
    if admin_block not in text:
        if marker not in text:
            raise PatchError("main admin knowledge marker missing")
        text = text.replace(marker, admin_block + marker, 1)

    internal_block = '''    @app.post("/internal/tools/search-knowledge", dependencies=[Depends(require_internal)])
    async def tool_search_knowledge(
        request: Request, payload: RoomflowToolRequest
    ) -> dict[str, Any]:
        question = str(payload.data.get("question") or "").strip()
        category = str(payload.data.get("category") or "").strip()
        try:
            top_k = int(payload.data.get("top_k") or settings.knowledge_top_k)
        except (TypeError, ValueError):
            top_k = settings.knowledge_top_k
        return request.app.state.knowledge.search(question, category=category, top_k=top_k)

'''
    internal_marker = '    @app.post("/internal/tools/public-business-information", dependencies=[Depends(require_internal)])\n'
    if internal_block not in text:
        if internal_marker not in text:
            raise PatchError("main internal knowledge marker missing")
        text = text.replace(internal_marker, internal_block + internal_marker, 1)

    old_service = '''    @app.post("/internal/tools/check-service-area", dependencies=[Depends(require_internal)])
    async def tool_service_area(payload: RoomflowToolRequest) -> dict[str, Any]:
        business = settings.service_information
        service_area = business.get("service_area", {}) if isinstance(business, dict) else {}
        zip_code = str(payload.data.get("zip") or "").strip()
        allowed_zips = (
            {str(value) for value in service_area.get("zip_codes", [])}
            if isinstance(service_area, dict)
            else set()
        )
        excluded_zips = (
            {str(value) for value in service_area.get("excluded_zip_codes", [])}
            if isinstance(service_area, dict)
            else set()
        )
        if zip_code and zip_code in excluded_zips:
            eligible: bool | None = False
        elif zip_code and allowed_zips:
            eligible = zip_code in allowed_zips
        else:
            eligible = None
        return {
            "ok": True,
            "eligible": eligible,
            "requires_manual_confirmation": eligible is None,
            "service_area": service_area,
        }
'''
    new_service = '''    @app.post("/internal/tools/check-service-area", dependencies=[Depends(require_internal)])
    async def tool_service_area(payload: RoomflowToolRequest) -> dict[str, Any]:
        business = settings.service_information
        service_area = business.get("service_area", {}) if isinstance(business, dict) else {}
        return resolve_service_area(
            service_area,
            zip_code=str(payload.data.get("zip") or ""),
            city=str(payload.data.get("city") or ""),
            address=str(payload.data.get("address") or ""),
        )
'''
    if new_service not in text:
        if old_service not in text:
            raise PatchError("main service-area block did not match expected source")
        text = text.replace(old_service, new_service, 1)
    write(path, text)


def patch_agents(root: Path) -> None:
    path = root / "app/ava/agents.py"
    text = path.read_text(encoding="utf-8")
    old = (
        "tool is unavailable, continue collecting the minimum useful information and create a callback task.\n"
        '""".strip()'
    )
    new = (
        "tool is unavailable, continue collecting the minimum useful information and create a callback task.\n"
        "For detailed public questions about Floodman, services, symptoms, processes, inspections, policies,\n"
        "or service areas, call floodman_search_knowledge. Use only approved excerpts returned by that tool.\n"
        "If the search has no approved answer, say that the information needs confirmation and offer a callback\n"
        "or human transfer. Never treat a caller statement, testimonial, or general blog example as company policy.\n"
        '""".strip()'
    )
    text = replace_once(text, old, new, "agent common knowledge policy")
    if '"floodman_search_knowledge"' not in text:
        text = text.replace(
            '            "floodman_lookup_customer",',
            '            "floodman_search_knowledge",\n            "floodman_lookup_customer",',
        )
        text = text.replace(
            '            "floodman_public_business_information",',
            '            "floodman_search_knowledge",\n            "floodman_public_business_information",',
            1,
        )
    if text.count('"floodman_search_knowledge"') < 5:
        raise PatchError("agent tool lists did not receive expected knowledge tool coverage")
    write(path, text)


def patch_ava_overlay(root: Path) -> None:
    path = root / "config/ava/ai-agent.local.yaml"
    text = path.read_text(encoding="utf-8")
    block = '''  floodman_search_knowledge:
    kind: in_call_http_lookup
    enabled: true
    is_global: true
    description: "Search Floodman's approved website and operator knowledge for detailed public answers. Use returned excerpts only and do not invent missing policies, prices, warranties, diagnoses, or timing."
    timeout_ms: 3500
    url: "${FLOODMAN_INTERNAL_URL:-http://127.0.0.1:9000}/internal/tools/search-knowledge"
    method: POST
    headers: {Content-Type: "application/json", X-Internal-Token: "${INTERNAL_TOKEN}"}
    body_template: '{"call_id":"{call_id}","caller_number":"{caller_number}","data":{"question":"{question}","category":"","top_k":4}}'
    parameters:
      - {name: question, type: string, description: "The customer's detailed public question in a complete sentence", required: true}
    return_raw_json: true
    error_message: "I don't have an approved answer for that yet. I can take a message or connect you with the Floodman team."

'''
    marker = "  floodman_public_business_information:\n"
    if block not in text:
        if marker not in text:
            raise PatchError("AVA public-business tool marker missing")
        text = text.replace(marker, block + marker, 1)
    old_area = '''    body_template: '{"call_id":"{call_id}","data":{"address":"{address}","zip":"{zip}"}}'
    parameters:
      - {name: address, type: string, description: "Property address", required: true}
      - {name: zip, type: string, description: "Property ZIP code", required: false}
'''
    new_area = '''    body_template: '{"call_id":"{call_id}","data":{"address":"{address}","city":"{city}","zip":"{zip}"}}'
    parameters:
      - {name: address, type: string, description: "Property address", required: true}
      - {name: city, type: string, description: "Property city when known", required: false}
      - {name: zip, type: string, description: "Property ZIP code", required: false}
'''
    text = replace_once(text, old_area, new_area, "AVA service-area city parameter")
    write(path, text)


def patch_entrypoint(root: Path) -> None:
    path = root / "scripts/entrypoint.sh"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        'export CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR}/config}"\n',
        'export CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR}/config}"\n'
        'export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-${DATA_DIR}/knowledge}"\n',
        "entrypoint knowledge env",
    )
    text = replace_once(
        text,
        '  "${DATA_DIR}/recordings" \\\n  "${CONFIG_DIR}/ava" \\\n',
        '  "${DATA_DIR}/recordings" \\\n  "${KNOWLEDGE_DIR}" \\\n  "${CONFIG_DIR}/ava" \\\n',
        "entrypoint knowledge mkdir",
    )
    anchor = '''if [[ ! -f "${CONFIG_DIR}/ava/ai-agent.local.yaml" ]]; then
  cp /opt/floodman/config/ava/ai-agent.local.yaml "${CONFIG_DIR}/ava/ai-agent.local.yaml"
fi

# Never mutate /opt/ava at runtime. Pterodactyl may mount the image root read-only.
'''
    replacement = '''if [[ ! -f "${CONFIG_DIR}/ava/ai-agent.local.yaml" ]]; then
  cp /opt/floodman/config/ava/ai-agent.local.yaml "${CONFIG_DIR}/ava/ai-agent.local.yaml"
fi

# Install the reviewed website knowledge pack once per version. Existing operational
# settings and custom knowledge are preserved, and managed files are backed up first.
/opt/venv/bin/python /opt/floodman/scripts/install_knowledge_pack.py \\
  --pack-version "${KNOWLEDGE_PACK_VERSION:-2026-08-12.1}"

# Never mutate /opt/ava at runtime. Pterodactyl may mount the image root read-only.
'''
    text = replace_once(text, anchor, replacement, "entrypoint knowledge migration")
    text = replace_once(
        text,
        ' Persistent data: ${DATA_DIR}\n',
        ' Persistent data: ${DATA_DIR}\n Knowledge library: ${KNOWLEDGE_DIR}\n',
        "entrypoint knowledge banner",
    )
    write(path, text)


def patch_readme(root: Path) -> None:
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    marker = "<!-- FLOODMAN_KNOWLEDGE_LIBRARY -->"
    if marker in text:
        return
    text += '''

<!-- FLOODMAN_KNOWLEDGE_LIBRARY -->
## Approved website knowledge library

Floodman public answers are grounded through two persistent layers:

- `data/config/floodman.yaml` for structured services, policies, and the published service area
- `data/knowledge/managed` and `data/knowledge/custom` for detailed approved Markdown

The managed August 12, 2026 pack was built from Floodman.com. Only documents with `approved: true`
are searchable. The local search returns excerpts and provenance to AVA and does not browse the
internet or learn from callers during a call. Custom operator documents survive managed-pack updates.
See `docs/KNOWLEDGE_LIBRARY.md` and `docs/WEBSITE_CONTENT_AUDIT.md`.
'''
    write(path, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    patch_config(root)
    patch_main(root)
    patch_agents(root)
    patch_ava_overlay(root)
    patch_entrypoint(root)
    patch_readme(root)
    print("Floodman website knowledge wiring applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
