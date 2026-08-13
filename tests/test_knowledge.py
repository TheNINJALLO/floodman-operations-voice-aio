from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from app.knowledge import KnowledgeBase, resolve_service_area


def test_knowledge_search_returns_approved_water_damage_document(project_root: Path) -> None:
    result = KnowledgeBase(project_root / "knowledge").search(
        "A pipe burst and there may be hidden water behind the wall. What does Floodman do?"
    )
    assert result["found"] is True
    assert result["answer_context"]
    assert any(item["category"] == "water_damage" for item in result["results"])
    assert all("source_url" in item for item in result["results"])


def test_unapproved_documents_are_not_returned(project_root: Path) -> None:
    result = KnowledgeBase(project_root / "knowledge").search(
        "What financing provider, office address, and exact warranty terms do you have?"
    )
    returned = {item["path"] for item in result["results"]}
    assert "99-review-needed.md" not in returned
    assert all("Operator Instructions" not in item["title"] for item in result["results"])


def test_knowledge_auto_reloads_custom_document(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    document = root / "custom.md"
    document.write_text(
        """---\ntitle: First Policy\ncategory: policy\napproved: true\ntags: [alpha]\nreviewed_at: 2026-08-12\nsummary: Alpha policy.\n---\n\nAlpha information.\n""",
        encoding="utf-8",
    )
    knowledge = KnowledgeBase(root)
    assert knowledge.search("alpha")["found"] is True
    document.write_text(
        """---\ntitle: Second Policy\ncategory: policy\napproved: true\ntags: [bravo]\nreviewed_at: 2026-08-12\nsummary: Bravo policy.\n---\n\nBravo information.\n""",
        encoding="utf-8",
    )
    assert knowledge.search("bravo")["found"] is True


def test_service_area_resolver_matches_published_city_and_address(project_root: Path) -> None:
    config = yaml.safe_load((project_root / "config/floodman.yaml").read_text(encoding="utf-8"))
    service_area = config["business"]["service_area"]
    city = resolve_service_area(service_area, city="Traverse City")
    assert city["eligible"] is True
    assert city["matched_by"] == "city"
    address = resolve_service_area(service_area, address="123 Main St, Grand Rapids, MI 49503")
    assert address["eligible"] is True
    assert address["matched_value"] == "Grand Rapids"
    street_name_only = resolve_service_area(service_area, address="123 Hart Ave, MI 49600")
    assert street_name_only["eligible"] is None
    unknown = resolve_service_area(service_area, city="Detroit")
    assert unknown["eligible"] is None
    assert unknown["requires_manual_confirmation"] is True


def test_repository_contains_complete_company_profile_and_wiring(project_root: Path) -> None:
    config = yaml.safe_load((project_root / "config/floodman.yaml").read_text(encoding="utf-8"))
    assert len(config["business"]["service_area"]["cities"]) == 148
    assert config["business"]["inspection_policy"]["free_inspection_advertised"] is True
    overlay = (project_root / "config/ava/ai-agent.local.yaml").read_text(encoding="utf-8")
    agents = (project_root / "app/ava/agents.py").read_text(encoding="utf-8")
    entrypoint = (project_root / "scripts/entrypoint.sh").read_text(encoding="utf-8")
    assert "floodman_search_knowledge:" in overlay
    assert "/internal/tools/search-knowledge" in overlay
    assert '"floodman_search_knowledge"' in agents
    assert "install_knowledge_pack.py" in entrypoint


def test_versioned_installer_preserves_operational_config_and_custom_documents(
    project_root: Path, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    config_dir = data / "config"
    custom = data / "knowledge/custom"
    custom.mkdir(parents=True)
    custom_file = custom / "approved-custom.md"
    custom_file.write_text("custom survives\n", encoding="utf-8")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "floodman.yaml").write_text(
        yaml.safe_dump(
            {
                "business": {"public_name": "stale"},
                "scheduling": {"lead_time_hours": 99},
                "roomflow": {"endpoints": {"lookup_customer": "/private/path"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    ava_dir = config_dir / "ava"
    ava_dir.mkdir()
    (ava_dir / "ai-agent.local.yaml").write_text(
        "custom_operator_key: preserved\n",
        encoding="utf-8",
    )
    command = [
        "python",
        str(project_root / "scripts/install_knowledge_pack.py"),
        "--pack-version",
        "test-pack",
        "--data-dir",
        str(data),
        "--config-dir",
        str(config_dir),
        "--knowledge-dir",
        str(data / "knowledge"),
        "--image-config",
        str(project_root / "config/floodman.yaml"),
        "--image-ava",
        str(project_root / "config/ava/ai-agent.local.yaml"),
        "--image-knowledge",
        str(project_root / "knowledge"),
    ]
    first = subprocess.run(command, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    installed = yaml.safe_load((config_dir / "floodman.yaml").read_text(encoding="utf-8"))
    assert installed["business"]["public_name"] == "Floodman"
    assert installed["scheduling"]["lead_time_hours"] == 99
    assert installed["roomflow"]["endpoints"]["lookup_customer"] == "/private/path"
    installed_ava = yaml.safe_load((ava_dir / "ai-agent.local.yaml").read_text(encoding="utf-8"))
    assert installed_ava["custom_operator_key"] == "preserved"
    assert "floodman_search_knowledge" in installed_ava["in_call_tools"]
    assert custom_file.read_text(encoding="utf-8") == "custom survives\n"
    assert (data / "knowledge/managed/10-water-damage-restoration.md").is_file()

    second = subprocess.run(command, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    assert "already_installed" in second.stdout
