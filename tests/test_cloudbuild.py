"""cloudbuild.yaml のデプロイ設定がコードと食い違わないことを確かめる。

渡し忘れた環境変数は本番で初めて落ち、余分な環境変数はどこからも読まれずに残る。
各サービスのコードが読む環境変数名を拾い、cloudbuild.yaml がそのサービスへ渡す名前と比べる。
"""

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_DIRS = ("monologue-uploader", "monologue-summarizer", "monologue-notifier")

# os.environ.get("X") / os.environ["X"] と、summarizer の必須変数ヘルパー _required("X") を拾う。
_READ_PATTERNS = [
    re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""),
    re.compile(r"""_required\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
]


def _steps() -> dict[str, dict]:
    config = yaml.safe_load((_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    return {step["dir"]: step for step in config["steps"]}


def _flag(step: dict, name: str) -> str | None:
    prefix = f"--{name}="
    values = [arg.removeprefix(prefix) for arg in step["args"] if arg.startswith(prefix)]
    return values[0] if values else None


def _names_passed(step: dict) -> set[str]:
    names: set[str] = set()
    for flag in ("set-env-vars", "set-secrets"):
        value = _flag(step, flag)
        if value:
            names.update(pair.split("=", 1)[0] for pair in value.split(","))
    return names


def _names_read_by_code(service_dir: str) -> set[str]:
    names: set[str] = set()
    for path in (_ROOT / service_dir).glob("*.py"):
        if path.name == "conftest.py":
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in _READ_PATTERNS:
            names.update(pattern.findall(source))
    return names


def test_全サービスを同名のCloudRunサービスへデプロイする():
    steps = _steps()
    assert set(steps) == set(_SERVICE_DIRS)
    for service_dir, step in steps.items():
        assert step["args"][:3] == ["run", "deploy", service_dir]


def test_エントリポイントの関数がサービスのmain_pyにある():
    for service_dir, step in _steps().items():
        function = _flag(step, "function")
        source = (_ROOT / service_dir / "main.py").read_text(encoding="utf-8")
        assert re.search(rf"^def {function}\(", source, re.MULTILINE), service_dir


def test_渡す環境変数とsecretがコードの読む名前と一致する():
    for service_dir, step in _steps().items():
        assert _names_passed(step) == _names_read_by_code(service_dir), service_dir


def test_ソースの送信から個人の置き場と認証ファイルを外す():
    ignored = (_ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
    # private/ には数百 MB の音声サンプルと個人の値がある。
    # gha-creds-*.json は GitHub Actions の auth が作業ディレクトリに置く資格情報。
    assert {"private/", "gha-creds-*.json"} <= set(ignored)
