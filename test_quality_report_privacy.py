import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_redacts_private_details_by_default(tmp_path):
    workdir = tmp_path / "private-directory"
    workdir.mkdir()
    (workdir / "script.huaben.jsonl").write_text(json.dumps({"type": "chapter", "id": "secret-id", "text": "private-title"}) + "\n", encoding="utf-8")
    script = Path(__file__).with_name("quality_report.py")
    def run(*options):
        return subprocess.check_output([sys.executable, str(script), str(workdir), "--book-id", "secret-book", *options], text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    output = run()
    assert all(value not in output for value in (str(workdir), "secret-id", "private-title", "secret-book"))
    assert json.loads(output)["overview"]["chapters_total"] == 1
    assert "private-title" in run("--include-private-details")

