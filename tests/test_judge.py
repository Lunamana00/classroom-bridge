import json
from pathlib import Path

import pytest

from classroom.judge import CodexClient, JudgeError

CANDIDATES = [{"chunk_id": 7, "material_id": 1, "title": "자료", "section": "정의", "page": 3, "text": "기울기 반대로 이동합니다.", "score": 0.6}]


def fake_codex(tmp_path, monkeypatch, body):
    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env python3\nimport sys, json, pathlib, time\n" + body)
    binary.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(binary))
    monkeypatch.setenv("CLASSROOM_MODE", "codex")
    return CodexClient()


async def test_cli_uses_stdin_and_validates_source_ids(tmp_path, monkeypatch):
    client = fake_codex(tmp_path, monkeypatch, """
assert '--ephemeral' in sys.argv and '--ignore-user-config' in sys.argv
assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'
payload = sys.stdin.read()
assert 'untrusted data' in payload
out = pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])
out.write_text(json.dumps({'selections':[{'chunk_id':7,'score':0.95}], 'sufficient':True}))
""")
    sentinel = tmp_path / "must-not-exist"
    selected, sufficient = await client.select(f"질문; $(touch {sentinel})", CANDIDATES)
    assert selected[0]["page"] == 3 and selected[0]["text"] == CANDIDATES[0]["text"]
    assert sufficient and not sentinel.exists()


@pytest.mark.parametrize("decision", [
    {"selections": [{"chunk_id": 999, "score": 0.9}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": True}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": "0.9"}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": 1.2}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": 0.9}] * 2, "sufficient": True},
])
async def test_bad_decisions_are_rejected(tmp_path, monkeypatch, decision):
    body = "out=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])\nout.write_text(" + repr(json.dumps(decision)) + ")\n"
    client = fake_codex(tmp_path, monkeypatch, body)
    with pytest.raises(JudgeError, match="올바른"):
        await client.select("질문", CANDIDATES)


async def test_cli_failure_does_not_leak_raw_logs(tmp_path, monkeypatch):
    client = fake_codex(tmp_path, monkeypatch, "sys.stderr.write('sensitive-provider-output')\nsys.exit(1)\n")
    with pytest.raises(JudgeError) as error:
        await client.select("질문", CANDIDATES)
    assert "sensitive" not in str(error.value)


async def test_cli_timeout_stops_the_child(tmp_path, monkeypatch):
    marker = tmp_path / "should-not-run"
    client = fake_codex(tmp_path, monkeypatch, f"time.sleep(10)\npathlib.Path({str(marker)!r}).touch()\n")
    client.timeout = 0.05
    with pytest.raises(JudgeError, match="초과"):
        await client.select("질문", CANDIDATES)
    assert not marker.exists()
