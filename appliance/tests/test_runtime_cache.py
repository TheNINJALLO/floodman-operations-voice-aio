import importlib.util
import os


def test_model_download_cache_overrides_readonly_home_and_stale_cache_paths(project_root, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cache_downloader", project_root / "scripts/download_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", "/root")
    names = ("XDG_CACHE_HOME", "HF_HOME", "HF_HUB_CACHE", "HF_XET_CACHE", "HF_ASSETS_CACHE", "TMPDIR")
    for name in names:
        monkeypatch.setenv(name, "/root/.cache/stale")
    data = tmp_path / "persistent"
    module.configure_runtime_cache(data)
    for name in names:
        path = module.Path(os.environ[name])
        assert path.is_relative_to(data)
        probe = path / "write-check"
        probe.write_text("persistent cache")
        assert probe.read_text() == "persistent cache"
