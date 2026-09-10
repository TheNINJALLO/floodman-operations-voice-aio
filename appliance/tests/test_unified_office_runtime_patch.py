from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _patch_module(project_root: Path):
    path = project_root / "unified" / "patch-office-runtime.py"
    spec = importlib.util.spec_from_file_location("patch_office_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owner_recovery_patch_is_scoped_and_idempotent(project_root: Path):
    module = _patch_module(project_root)
    source = '''                existing["gauzy_role"] = str(identity.get("gauzy_role") or existing.get("gauzy_role") or "USER")
                existing["auth_source"] = "GAUZY" if not existing.get("password_hash") else "LOCAL_AND_GAUZY"
            if role not in {"OWNER", "ADMIN", "OFFICE_MANAGER", "BILLING", "ESTIMATOR", "TECHNICIAN", "VIEWER"}:
                role = "VIEWER"
            user = {
                "password_hash": "",
'''

    patched = module.patch_store(source)

    assert 'email_key == configured_owner_email' in patched
    assert 'existing.get("role") == "OWNER"' in patched
    assert 'and len(configured_owner_password) >= 10' in patched
    assert '"password_hash": local_password_hash' in patched
    assert module.patch_store(patched) == patched


def test_sidebar_placeholder_patch_redirects_to_real_workspace(project_root: Path):
    module = _patch_module(project_root)
    source = '''@app.get("/office")
@app.get("/office/desktop")
async def office_dashboard():
    pass
'''

    patched = module.patch_main(source)

    assert '@app.get("/office/labelHere", include_in_schema=False)' in patched
    assert 'RedirectResponse("/office/desktop", status_code=303)' in patched
    assert module.patch_main(patched) == patched


def test_runtime_patch_refuses_an_unknown_upstream_shape(project_root: Path):
    module = _patch_module(project_root)

    with pytest.raises(RuntimeError, match="Office dashboard route"):
        module.patch_main("upstream route changed")
