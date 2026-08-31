from pathlib import Path


def test_renderer_detects_packaged_asterisk_modules(
    project_root: Path,
) -> None:
    source = (
        project_root
        / "scripts/render_asterisk.py"
    ).read_text(encoding="utf-8")

    assert "def detect_asterisk_module_dir()" in source
    assert (
        "/usr/lib/x86_64-linux-gnu/asterisk/modules"
        in source
    )
    assert "app_audiosocket.so" in source
    assert "astmoddir => {module_dir}" in source
    assert (
        "astmoddir => /usr/lib/asterisk/modules"
        not in source
    )
