import importlib.util
from pathlib import Path
import pytest

def load(project_root):
    path=project_root/"scripts/envfile.py"
    spec=importlib.util.spec_from_file_location("envfile_test",path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_envfile_fails_closed(project_root,tmp_path):
    module=load(project_root);path=tmp_path/"runtime.env";path.write_text("export A='broken\n")
    with pytest.raises(module.EnvFileError): module.parse(path)
    assert path.read_text()=="export A='broken\n"

def test_envfile_deduplicates(project_root,tmp_path):
    module=load(project_root);path=tmp_path/"runtime.env";path.write_text("export A=one\nexport A=two\n")
    values=module.normalize(path,{"B":"three"})
    assert values["A"]=="two"
    text=path.read_text();assert text.count("export A=")==1;assert "export B=three" in text
