from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CallRegistry:
    def __init__(self, runtime_dir: Path):
        self.pre_dir = runtime_dir / "precall"
        self.action_dir = runtime_dir / "actions"
        self.pre_dir.mkdir(parents=True, exist_ok=True)
        self.action_dir.mkdir(parents=True, exist_ok=True)

    def _pre(self, call_uuid: str) -> Path:
        return self.pre_dir / f"{call_uuid}.json"

    def _action(self, call_uuid: str) -> Path:
        return self.action_dir / f"{call_uuid}.json"

    def read_pre(self, call_uuid: str) -> dict[str, Any]:
        path = self._pre(call_uuid)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        finally:
            path.unlink(missing_ok=True)

    def write_action(self, call_uuid: str, action: str, number: str = "", reason: str = "") -> None:
        path = self._action(call_uuid)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"action": action, "number": number, "reason": reason}), encoding="utf-8")
        temporary.replace(path)
