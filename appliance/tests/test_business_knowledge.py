from pathlib import Path
from app.business import BusinessDirectory
from app.knowledge import KnowledgeBase

def test_service_area(project_root: Path):
    directory = BusinessDirectory(project_root / "config/service_area.yaml")
    assert directory.service_area("Grand Rapids, Michigan").status == "published"
    assert directory.service_area("Honolulu, Hawaii").status == "manual_confirmation"

def test_approved_knowledge_only(project_root: Path):
    knowledge = KnowledgeBase(project_root / "knowledge")
    assert knowledge.documents
    assert all(document.path.name != "99-review-needed.md" for document in knowledge.documents)
    assert knowledge.search("water damage electricity")
