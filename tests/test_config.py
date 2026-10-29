import os

from classroom.config import load_environment


def test_local_file_precedence_without_overriding_process(tmp_path, monkeypatch):
    monkeypatch.setattr("classroom.config.ROOT", tmp_path)
    (tmp_path / ".env").write_text("OPEN_API_KEY=base-key\nCODEX_MODEL=base-model\nCLASSROOM_MODE=jev\n")
    (tmp_path / ".env.local").write_text("OPEN_API_KEY=local-key\nCODEX_MODEL=local-model\n")
    monkeypatch.delenv("OPEN_API_KEY")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    load_environment()
    assert os.environ["OPEN_API_KEY"] == "local-key"
    assert os.environ["CODEX_MODEL"] == "local-model"
    assert os.environ["CLASSROOM_MODE"] == "demo"
