#!/usr/bin/env python3
"""Reject sensitive filenames in the Git index and reachable Git history."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_DIRS = {
    "backup",
    "backups",
    "dumps",
    "secrets",
}

FORBIDDEN_EXTENSIONS = {
    ".sql",
    ".dump",
    ".backup",
    ".bak",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}

FORBIDDEN_NAMES = {
    "id_rsa",
    "id_ed25519",
    ".envrc",
    "credentials.json",
    "service-account.json",
}


def forbidden_reason(path: str) -> str | None:
    parts = [
        component.casefold()
        for component in PurePosixPath(path).parts
    ]

    if not parts:
        return None

    name = parts[-1]

    if any(
        component in FORBIDDEN_DIRS
        for component in parts[:-1]
    ):
        return "sensitive directory"

    if name == ".env" or (
        name.startswith(".env.")
        and not name.endswith(".example")
    ):
        return "environment configuration"

    if name in FORBIDDEN_NAMES:
        return "credential file"

    if any(
        name.endswith(extension)
        for extension in FORBIDDEN_EXTENSIONS
    ):
        return "database dump or private-key file"

    return None


def git_paths(*args: str) -> set[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    return {
        entry.decode(
            "utf-8",
            errors="surrogateescape",
        )
        for entry in completed.stdout.split(b"\0")
        if entry.strip()
    }


def run_checks() -> int:
    try:
        staged = git_paths(
            "ls-files",
            "--cached",
            "-z",
        )

        history = git_paths(
            "log",
            "--all",
            "--name-only",
            "--pretty=format:",
            "-z",
        )

    except (OSError, subprocess.CalledProcessError) as error:
        print(
            f"ERROR: unable to inspect Git paths: {error}",
            file=sys.stderr,
        )
        return 2

    failures: dict[str, tuple[str, str]] = {}

    for source, names in (
        ("index", staged),
        ("history", history),
    ):
        for name in names:
            reason = forbidden_reason(name)

            if reason:
                failures[name] = (source, reason)

    if failures:
        print(
            "ERROR: sensitive or backup files detected in Git:",
            file=sys.stderr,
        )

        for name, (source, reason) in sorted(failures.items()):
            print(
                f" - {name} ({source}: {reason})",
                file=sys.stderr,
            )

        print(
            "Remove forbidden paths from Git and, "
            "if necessary, clean history before merging.",
            file=sys.stderr,
        )

        return 1

    print(
        "Repository safety: OK "
        f"({len(staged)} indexed paths, "
        f"{len(history)} historical paths checked)"
    )

    return 0


def self_test() -> None:
    allowed = [
        ".env.example",
        ".env.staging.example",
        ".github/workflows/ci.yml",
        "backend/fastapi/migrations/versions/0001.py",
    ]

    blocked = [
        ".env",
        ".env.local",
        "var/backups/x.sql",
        "var/backups/x.txt",
        "dump.sql",
        "private.pem",
        "secrets/access.json",
        "id_rsa",
        "data.sqlite3",
    ]

    for name in allowed:
        assert forbidden_reason(name) is None, name

    for name in blocked:
        assert forbidden_reason(name), name

    print("Repository safety self-test: OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()

    elif len(sys.argv) == 1:
        sys.exit(run_checks())

    else:
        print(
            "Usage: check_repo_safety.py [--self-test]",
            file=sys.stderr,
        )
        sys.exit(2)
