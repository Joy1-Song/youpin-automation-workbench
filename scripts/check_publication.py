"""Check publishable files without printing any matched secret values."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".idea", "state", "logs"}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    "filled environment credential": re.compile(
        r"(?m)^(?:YOUPIN_(?:TOKEN|UK|COOKIE|DEVICE_TOKEN|DEVICE_UK)|SMTP_PASSWORD)[ \t]*=[ \t]*[^\s#][^\r\n]{5,}"
    ),
}


def publication_files() -> list[Path]:
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
        )
        return [ROOT / name.decode("utf-8") for name in result.stdout.split(b"\0") if name]
    return [p for p in ROOT.rglob("*") if p.is_file()
            and not any(part in EXCLUDED_DIRS for part in p.relative_to(ROOT).parts)]


def forbidden_path(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    name = path.name.lower()
    return (
        any(part in EXCLUDED_DIRS for part in relative.parts)
        or (name.startswith(".env") and name != ".env.example")
        or name.startswith("config.local")
        or ".bak" in name
        or path.suffix.lower() in {".log", ".db", ".sqlite", ".apk", ".img", ".qcow2", ".pem", ".key", ".docx"}
    )


def private_env_values(path: Path | None) -> list[str]:
    if path is None:
        return []
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.lstrip().startswith("#"):
            continue
        if not any(word in key.upper() for word in ("TOKEN", "COOKIE", "PASSWORD", "USER", "UK", "UUID", "FROM")):
            continue
        value = value.strip().strip("\"'")
        if len(value) >= 8:
            values.append(value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-env", type=Path, help="Optional local private env, read only for exact-value comparison")
    args = parser.parse_args()
    private_values = private_env_values(args.compare_env)
    files = publication_files()
    findings = []
    if not files:
        findings.append("No publishable files found; stage files before checking a new Git repository")
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if forbidden_path(path):
            findings.append(f"{relative}: forbidden file")
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            findings.append(f"{relative}: binary file requires manual review")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{relative}: possible {label}")
        if any(value in text for value in private_values):
            findings.append(f"{relative}: matches a private environment value")

    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    if not config.get("dry_run") or config.get("purchase_automation", {}).get("enabled"):
        findings.append("config.example.json: unsafe startup defaults")
    for line in findings:
        print(line)
    print(f"Checked {len(files)} publication files; {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
