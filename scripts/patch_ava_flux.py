#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import textwrap
from pathlib import Path


MARKER = "Floodman Flux TurnInfo protocol patch"
LEGACY_MARKER = "Floodman Flux v2 query contract patch"


class PatchError(RuntimeError):
    pass


def insert_after(
    source: str,
    anchor: str,
    addition: str,
    label: str,
) -> str:
    if addition.strip() in source:
        return source
    count = source.count(anchor)
    if count != 1:
        raise PatchError(
            f"{label}: expected one anchor, found {count}"
        )
    return source.replace(anchor, anchor + addition, 1)


def replace_method(
    source: str,
    *,
    class_name: str,
    method_name: str,
    replacement: str,
) -> str:
    tree = ast.parse(source)
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == class_name
        ),
        None,
    )
    if class_node is None:
        raise PatchError(f"class {class_name!r} was not found")

    method_node = next(
        (
            node
            for node in class_node.body
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and node.name == method_name
        ),
        None,
    )
    if method_node is None or method_node.end_lineno is None:
        raise PatchError(
            f"method {class_name}.{method_name} was not found"
        )

    lines = source.splitlines(keepends=True)
    original_line = lines[method_node.lineno - 1]
    indent = original_line[
        : len(original_line) - len(original_line.lstrip())
    ]
    rendered = textwrap.indent(
        textwrap.dedent(replacement).strip("\n"),
        indent,
    ) + "\n"
    lines[
        method_node.lineno - 1 : method_node.end_lineno
    ] = rendered.splitlines(keepends=True)
    return "".join(lines)


HANDSHAKE_HELPER = r'''

# Floodman Flux TurnInfo protocol patch.
# Preserve the older marker because the container build verifies it.
# Floodman Flux v2 query contract patch.
def _flux_handshake_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        body_text = body.decode("utf-8", errors="replace").strip()
    else:
        body_text = str(body or "").strip()
    request_id = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            request_id = (
                headers.get("x-dg-request-id")
                or headers.get("dg-request-id")
                or ""
            )
        except Exception:
            request_id = ""
    pieces = []
    if status is not None:
        pieces.append(f"HTTP {status}")
    if request_id:
        pieces.append(f"request_id={request_id}")
    if body_text:
        pieces.append(body_text[:800])
    if not pieces:
        pieces.append(str(exc))
    return ": ".join(pieces)
'''


VALIDATE_CONNECTIVITY = r'''
    async def validate_connectivity(
        self,
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            return {
                "healthy": False,
                "error": "Deepgram Flux requires an API key",
                "details": {},
            }

        model = str(
            merged.get("model") or "flux-general-en"
        )
        base_url = _normalize_ws_url(
            merged.get("base_url")
        )
        query_items = [
            ("model", model),
            (
                "encoding",
                str(merged.get("encoding") or "linear16"),
            ),
            (
                "sample_rate",
                str(merged.get("sample_rate") or "16000"),
            ),
        ]
        if model == "flux-general-multi":
            language_hint = (
                merged.get("language_hint")
                or merged.get("language")
            )
            if language_hint:
                if isinstance(language_hint, (list, tuple)):
                    query_items.extend(
                        (
                            "language_hint",
                            str(item).split("-", 1)[0],
                        )
                        for item in language_hint
                        if str(item).strip()
                    )
                else:
                    query_items.append(
                        (
                            "language_hint",
                            str(language_hint).split("-", 1)[0],
                        )
                    )

        parsed = urlparse(base_url)
        ws_url = urlunparse(
            parsed._replace(
                query=urlencode(query_items, doseq=True)
            )
        )
        started_at = time.perf_counter()

        try:
            async with websockets.connect(
                ws_url,
                additional_headers={
                    "Authorization": f"Token {api_key}",
                },
                user_agent_header=(
                    "Floodman-Voice-AIO/Flux-v2"
                ),
                compression=None,
                proxy=None,
                open_timeout=12,
                close_timeout=3,
                ping_interval=None,
                max_size=2 * 1024 * 1024,
            ) as websocket:
                connected_raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=5.0,
                )
                connected = json.loads(connected_raw)
                if connected.get("type") != "Connected":
                    raise RuntimeError(
                        "Expected Deepgram Connected message, "
                        f"received {connected!r}"
                    )

                thresholds = {
                    "eot_threshold": float(
                        merged.get(
                            "eot_threshold",
                            0.7,
                        )
                    ),
                    "eot_timeout_ms": int(
                        merged.get(
                            "eot_timeout_ms",
                            5000,
                        )
                    ),
                }
                eager = merged.get("eager_eot_threshold")
                if eager is not None:
                    thresholds["eager_eot_threshold"] = float(
                        eager
                    )

                await websocket.send(
                    json.dumps(
                        {
                            "type": "Configure",
                            "thresholds": thresholds,
                        }
                    )
                )
                configured_raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=5.0,
                )
                configured = json.loads(configured_raw)
                if configured.get("type") not in {
                    "ConfigureSuccess",
                    "Connected",
                }:
                    raise RuntimeError(
                        "Deepgram Configure failed: "
                        f"{configured!r}"
                    )

            return {
                "healthy": True,
                "error": None,
                "details": {
                    "endpoint": ws_url,
                    "protocol": "websocket",
                    "model": model,
                    "latency_ms": round(
                        (
                            time.perf_counter()
                            - started_at
                        )
                        * 1000.0,
                        2,
                    ),
                },
            }
        except Exception as exc:
            detail = _flux_handshake_error(exc)
            return {
                "healthy": False,
                "error": (
                    "Deepgram Flux validation failed: "
                    f"{detail}"
                ),
                "details": {
                    "endpoint": ws_url,
                    "exception": detail,
                },
            }
'''


OPEN_CALL = r'''
    async def open_call(
        self,
        call_id: str,
        options: Dict[str, Any],
    ) -> None:
        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            raise RuntimeError(
                "Deepgram Flux STT requires an API key"
            )

        model = str(
            merged.get("model") or "flux-general-en"
        )
        query_items = [
            ("model", model),
            (
                "encoding",
                str(merged.get("encoding") or "linear16"),
            ),
            (
                "sample_rate",
                str(merged.get("sample_rate") or "16000"),
            ),
        ]
        if model == "flux-general-multi":
            language_hint = (
                merged.get("language_hint")
                or merged.get("language")
            )
            if language_hint:
                if isinstance(language_hint, (list, tuple)):
                    query_items.extend(
                        (
                            "language_hint",
                            str(item).split("-", 1)[0],
                        )
                        for item in language_hint
                        if str(item).strip()
                    )
                else:
                    query_items.append(
                        (
                            "language_hint",
                            str(language_hint).split("-", 1)[0],
                        )
                    )

        ws_url = _normalize_ws_url(
            merged.get("base_url")
        )
        parsed = urlparse(ws_url)
        ws_url = urlunparse(
            parsed._replace(
                query=urlencode(query_items, doseq=True)
            )
        )

        logger.info(
            "Deepgram Flux STT opening session",
            call_id=call_id,
            url=ws_url,
            component=self.component_key,
        )

        try:
            websocket = await websockets.connect(
                ws_url,
                additional_headers={
                    "Authorization": f"Token {api_key}",
                },
                user_agent_header=(
                    "Floodman-Voice-AIO/Flux-v2"
                ),
                compression=None,
                proxy=None,
                open_timeout=12,
                max_size=16 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            )

            connected_raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=5.0,
            )
            connected = json.loads(connected_raw)
            if connected.get("type") != "Connected":
                raise RuntimeError(
                    "Expected Deepgram Connected message, "
                    f"received {connected!r}"
                )

            thresholds = {
                "eot_threshold": float(
                    merged.get(
                        "eot_threshold",
                        0.7,
                    )
                ),
                "eot_timeout_ms": int(
                    merged.get(
                        "eot_timeout_ms",
                        5000,
                    )
                ),
            }
            eager = merged.get("eager_eot_threshold")
            if eager is not None:
                thresholds["eager_eot_threshold"] = float(
                    eager
                )

            await websocket.send(
                json.dumps(
                    {
                        "type": "Configure",
                        "thresholds": thresholds,
                    }
                )
            )
            configured_raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=5.0,
            )
            configured = json.loads(configured_raw)
            if configured.get("type") != "ConfigureSuccess":
                raise RuntimeError(
                    "Deepgram Configure failed: "
                    f"{configured!r}"
                )
        except Exception as exc:
            try:
                await websocket.close()
            except Exception:
                pass
            detail = _flux_handshake_error(exc)
            logger.error(
                "Failed to connect to Deepgram Flux",
                call_id=call_id,
                error=detail,
                exc_info=True,
            )
            raise RuntimeError(
                "Deepgram Flux connection failed: "
                f"{detail}"
            ) from exc

        session_id = str(
            connected.get("request_id")
            or uuid.uuid4()
        )
        session = _FluxSessionState(
            websocket=websocket,
            options=merged,
            session_id=session_id,
        )
        self._sessions[call_id] = session
        session.receiver_task = asyncio.create_task(
            self._receive_loop(call_id, session)
        )

        logger.info(
            "Deepgram Flux STT session opened",
            call_id=call_id,
            session_id=session_id,
            model=model,
            thresholds=thresholds,
        )
'''


CLOSE_CALL = r'''
    async def close_call(self, call_id: str) -> None:
        session = self._sessions.pop(call_id, None)
        if not session:
            return

        session.active = False
        if (
            session.receiver_task
            and not session.receiver_task.done()
        ):
            session.receiver_task.cancel()
            try:
                await session.receiver_task
            except asyncio.CancelledError:
                pass

        try:
            session.transcript_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        try:
            await session.websocket.send(
                json.dumps({"type": "CloseStream"})
            )
        except Exception:
            pass
        try:
            await session.websocket.close()
        except Exception:
            pass

        logger.info(
            "Deepgram Flux STT session closed",
            call_id=call_id,
            session_id=session.session_id,
        )
'''


RECEIVE_LOOP = r'''
    async def _receive_loop(
        self,
        call_id: str,
        session: _FluxSessionState,
    ) -> None:
        try:
            async for message in session.websocket:
                if not session.active:
                    break

                try:
                    data = (
                        json.loads(message)
                        if isinstance(message, str)
                        else message
                    )
                except (json.JSONDecodeError, TypeError):
                    continue

                msg_type = data.get("type")
                if msg_type == "TurnInfo":
                    event = str(data.get("event") or "")
                    transcript = self._extract_transcript(
                        data
                    )

                    if event == "EndOfTurn":
                        logger.info(
                            "Deepgram Flux end of turn detected",
                            call_id=call_id,
                            transcript_preview=(
                                transcript[:80]
                                if transcript
                                else ""
                            ),
                        )
                        if transcript:
                            try:
                                session.transcript_queue.put_nowait(
                                    transcript
                                )
                            except asyncio.QueueFull:
                                try:
                                    session.transcript_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                await session.transcript_queue.put(
                                    transcript
                                )
                        session.turn_complete_event.set()

                    elif event == "EagerEndOfTurn":
                        logger.debug(
                            "Deepgram Flux eager end of turn",
                            call_id=call_id,
                            transcript_preview=(
                                transcript[:80]
                                if transcript
                                else ""
                            ),
                        )

                    elif event in {
                        "StartOfTurn",
                        "TurnResumed",
                    }:
                        session.turn_complete_event.clear()
                        logger.debug(
                            "Deepgram Flux turn active",
                            call_id=call_id,
                            event=event,
                        )

                    else:
                        logger.debug(
                            "Deepgram Flux TurnInfo",
                            call_id=call_id,
                            event=event,
                        )

                elif msg_type == "Results":
                    # Backward-compatible handling for older
                    # Deepgram response shapes.
                    transcript = self._extract_transcript(
                        data
                    )
                    if (
                        transcript
                        and data.get("is_final", False)
                    ):
                        await session.transcript_queue.put(
                            transcript
                        )

                elif msg_type in {
                    "Connected",
                    "ConfigureSuccess",
                }:
                    continue

                elif msg_type in {
                    "ConfigureFailure",
                    "Error",
                }:
                    raise RuntimeError(
                        "Deepgram Flux runtime error: "
                        f"{data.get('code') or msg_type}: "
                        f"{data.get('description') or data}"
                    )

                else:
                    logger.debug(
                        "Deepgram Flux message",
                        call_id=call_id,
                        type=msg_type,
                    )

        except websockets.ConnectionClosed as exc:
            logger.info(
                "Deepgram Flux websocket closed",
                call_id=call_id,
                code=getattr(exc, "code", None),
                reason=getattr(exc, "reason", None),
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "Deepgram Flux receive loop error",
                call_id=call_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            session.active = False
            try:
                session.transcript_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
'''


EXTRACT_TRANSCRIPT = r'''
    def _extract_transcript(
        self,
        message: Dict[str, Any],
    ) -> Optional[str]:
        direct = message.get("transcript")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        try:
            channel = message.get("channel", {})
            alternatives = channel.get(
                "alternatives",
                [],
            )
            if alternatives:
                return str(
                    alternatives[0].get(
                        "transcript",
                        "",
                    )
                ).strip()
        except (
            KeyError,
            IndexError,
            AttributeError,
        ):
            pass
        return None
'''


def patch(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False

    source = insert_after(
        source,
        "logger = get_logger(__name__)\n",
        HANDSHAKE_HELPER,
        "Flux handshake helper",
    )
    for name, replacement in (
        (
            "validate_connectivity",
            VALIDATE_CONNECTIVITY,
        ),
        ("open_call", OPEN_CALL),
        ("close_call", CLOSE_CALL),
        ("_receive_loop", RECEIVE_LOOP),
        ("_extract_transcript", EXTRACT_TRANSCRIPT),
    ):
        source = replace_method(
            source,
            class_name="DeepgramFluxSTTAdapter",
            method_name=name,
            replacement=replacement,
        )

    ast.parse(source)
    path.write_text(
        source,
        encoding="utf-8",
        newline="\n",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ava-root",
        type=Path,
        default=Path("/opt/ava"),
    )
    args = parser.parse_args()
    path = (
        args.ava_root.resolve()
        / "src/pipelines/deepgram_flux.py"
    )
    if not path.is_file():
        raise PatchError(
            f"required AVA source is missing: {path}"
        )
    if patch(path):
        print(f"patched {path}")
    else:
        print("Floodman Flux TurnInfo patch already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
