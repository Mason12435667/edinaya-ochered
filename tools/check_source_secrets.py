#!/usr/bin/env python3
"""High-confidence source-tree secret check for Единая очередь.

It intentionally ignores runtime/data directories and reports only likely literal
credentials. Exit code 1 means a likely secret should be moved to the external
systemd secrets file before committing the project.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "data", "chat-media", "outbound-media", ".wwebjs_auth", ".wwebjs_cache", "__pycache__"}
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml", ".ini", ".conf", ".service", ".sh"}
PLACEHOLDERS = {"changeme", "change-me", "example", "placeholder", "your-token", "your_token", "<token>", "<secret>", "<password>", ""}
ASSIGN = re.compile(
    r'''(?ix)\b([A-Z0-9_]*(?:TOKEN|API_KEY|SECRET|PASSWORD|PRIVATE_KEY|CLIENT_SECRET|ACCESS_KEY)[A-Z0-9_]*)\s*[:=]\s*["']([^"']{12,})["']'''
)
COMMON = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b\d{6,12}:[0-9A-Za-z_-]{30,}\b"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = ASSIGN.search(line)
            if m:
                value = m.group(2).strip().lower()
                if value not in PLACEHOLDERS and "os.getenv" not in line and "process.env" not in line:
                    findings.append(f"{rel}:{lineno}: вероятный literal secret ({m.group(1)})")
            if any(rx.search(line) for rx in COMMON):
                findings.append(f"{rel}:{lineno}: вероятный credential/token")
    if findings:
        print("Найдены возможные секреты в исходниках:")
        for item in findings[:100]:
            print(" -", item)
        return 0 if args.warn_only else 1
    print("SOURCE_SECRET_SCAN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
