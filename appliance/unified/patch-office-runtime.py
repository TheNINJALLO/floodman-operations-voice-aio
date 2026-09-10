#!/usr/bin/env python3
"""Apply verified unified-runtime fixes to the pinned Floodman Office source."""

from __future__ import annotations

import argparse
from pathlib import Path


OWNER_RECOVERY_MARKER = "configured_owner_password = os.getenv(\"FLOODMAN_OWNER_PASSWORD\", \"\")"
PLACEHOLDER_ROUTE_MARKER = '@app.get("/office/labelHere", include_in_schema=False)'


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    matches = source.count(old)
    if matches != 1:
        raise RuntimeError(f"Expected one {label} anchor; found {matches}.")
    return source.replace(old, new, 1)


def patch_store(source: str) -> str:
    """Give only the configured installation Owner a local recovery password."""
    if OWNER_RECOVERY_MARKER in source:
        return source

    existing_anchor = '''                existing["gauzy_role"] = str(identity.get("gauzy_role") or existing.get("gauzy_role") or "USER")
                existing["auth_source"] = "GAUZY" if not existing.get("password_hash") else "LOCAL_AND_GAUZY"
'''
    existing_replacement = '''                existing["gauzy_role"] = str(identity.get("gauzy_role") or existing.get("gauzy_role") or "USER")
                configured_owner_email = os.getenv("FLOODMAN_OWNER_EMAIL", "").strip().lower()
                configured_owner_password = os.getenv("FLOODMAN_OWNER_PASSWORD", "")
                if (
                    email_key == configured_owner_email
                    and existing.get("role") == "OWNER"
                    and not existing.get("password_hash")
                    and len(configured_owner_password) >= 10
                ):
                    existing["password_hash"] = hash_password(configured_owner_password)
                existing["auth_source"] = "GAUZY" if not existing.get("password_hash") else "LOCAL_AND_GAUZY"
'''
    source = _replace_once(
        source,
        existing_anchor,
        existing_replacement,
        label="existing Owner recovery",
    )

    new_user_anchor = '''            if role not in {"OWNER", "ADMIN", "OFFICE_MANAGER", "BILLING", "ESTIMATOR", "TECHNICIAN", "VIEWER"}:
                role = "VIEWER"
            user = {
'''
    new_user_replacement = '''            if role not in {"OWNER", "ADMIN", "OFFICE_MANAGER", "BILLING", "ESTIMATOR", "TECHNICIAN", "VIEWER"}:
                role = "VIEWER"
            configured_owner_password = os.getenv("FLOODMAN_OWNER_PASSWORD", "")
            local_password_hash = (
                hash_password(configured_owner_password)
                if role == "OWNER"
                and email_key == os.getenv("FLOODMAN_OWNER_EMAIL", "").strip().lower()
                and len(configured_owner_password) >= 10
                else ""
            )
            user = {
'''
    source = _replace_once(
        source,
        new_user_anchor,
        new_user_replacement,
        label="new Owner recovery",
    )
    return _replace_once(
        source,
        '                "password_hash": "",\n',
        '                "password_hash": local_password_hash,\n',
        label="new Owner password hash",
    )


def patch_main(source: str) -> str:
    """Fail open from a leaked UI placeholder to the real Office workspace."""
    if PLACEHOLDER_ROUTE_MARKER in source:
        return source
    anchor = '''@app.get("/office")
@app.get("/office/desktop")
'''
    replacement = '''@app.get("/office/labelHere", include_in_schema=False)
def legacy_sidebar_placeholder() -> RedirectResponse:
    return RedirectResponse("/office/desktop", status_code=303)


@app.get("/office")
@app.get("/office/desktop")
'''
    return _replace_once(source, anchor, replacement, label="Office dashboard route")


def patch_file(path: Path, transform) -> None:
    original = path.read_text(encoding="utf-8")
    patched = transform(original)
    if patched != original:
        path.write_text(patched, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--office-root", type=Path, required=True)
    args = parser.parse_args()
    app_root = args.office_root / "app"
    patch_file(app_root / "store.py", patch_store)
    patch_file(app_root / "main.py", patch_main)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
