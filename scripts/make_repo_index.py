#!/usr/bin/env python3
"""Build a safe Markdown index of the repository for LLM review.

The script is intentionally conservative: it skips secrets, runtime data,
binary assets, logs, database dumps, virtual environments and generated caches.
It is safe to run from the repository root on Windows, Linux or macOS.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TEXT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".dockerignore",
    ".env.example",
    ".example",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SPECIAL_TEXT_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "Procfile",
}

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "venv",
}

DEFAULT_EXCLUDED_GLOBS = {
    "*.7z",
    "*.bak",
    "*.bin",
    "*.db",
    "*.dump",
    "*.gz",
    "*.jpeg",
    "*.jpg",
    "*.log",
    "*.pdf",
    "*.png",
    "*.pyc",
    "*.sqlite",
    "*.sqlite3",
    "*.tar",
    "*.zip",
    ".env",
    ".env.*",
    "*secret*",
    "*token*",
    "*password*",
    "*private*key*",
}

LANG_BY_EXT = {
    ".cfg": "ini",
    ".conf": "nginx",
    ".css": "css",
    ".csv": "csv",
    ".html": "html",
    ".ini": "ini",
    ".js": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".py": "python",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class IndexedFile:
    path: Path
    rel: str
    size: int
    sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a safe Markdown repository index.")
    parser.add_argument("--root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument(
        "--output",
        default="aemr-bot-index.md",
        help="Output Markdown file. Defaults to aemr-bot-index.md.",
    )
    parser.add_argument(
        "--max-file-kb",
        type=int,
        default=160,
        help="Skip individual text files larger than this size. Default: 160 KB.",
    )
    parser.add_argument(
        "--tree-only",
        action="store_true",
        help="Write only the file tree, without file contents.",
    )
    return parser.parse_args()


def normalize_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_excluded_by_glob(rel: str) -> bool:
    name = Path(rel).name
    lowered_rel = rel.lower()
    lowered_name = name.lower()
    for pattern in DEFAULT_EXCLUDED_GLOBS:
        lowered_pattern = pattern.lower()
        if fnmatch.fnmatch(lowered_name, lowered_pattern) or fnmatch.fnmatch(
            lowered_rel, lowered_pattern
        ):
            if lowered_name == ".env.example":
                return False
            return True
    return False


def is_text_candidate(path: Path) -> bool:
    if path.name in SPECIAL_TEXT_FILENAMES:
        return True
    if path.name == ".env.example":
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in TEXT_EXTENSIONS


def looks_binary(path: Path, sample_size: int = 4096) -> bool:
    try:
        chunk = path.read_bytes()[:sample_size]
    except OSError:
        return True
    return b"\x00" in chunk


def should_skip(path: Path, root: Path, max_file_bytes: int, output_path: Path) -> tuple[bool, str]:
    rel = normalize_rel(path, root)
    parts = set(path.relative_to(root).parts)

    if path.resolve() == output_path.resolve():
        return True, "output file"
    if DEFAULT_EXCLUDED_DIRS.intersection(parts):
        return True, "excluded directory"
    if is_excluded_by_glob(rel):
        return True, "excluded glob"
    if not is_text_candidate(path):
        return True, "non-text extension"
    try:
        size = path.stat().st_size
    except OSError:
        return True, "unreadable"
    if size > max_file_bytes:
        return True, f"larger than {max_file_bytes} bytes"
    if looks_binary(path):
        return True, "binary content"
    return False, ""


def iter_files(root: Path, max_file_bytes: int, output_path: Path) -> tuple[list[IndexedFile], list[str]]:
    indexed: list[IndexedFile] = []
    skipped: list[str] = []

    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDED_DIRS)

        for filename in sorted(filenames):
            path = current / filename
            rel = normalize_rel(path, root)
            skip, reason = should_skip(path, root, max_file_bytes, output_path)
            if skip:
                skipped.append(f"- `{rel}` — {reason}")
                continue
            data = path.read_bytes()
            indexed.append(
                IndexedFile(
                    path=path,
                    rel=rel,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )

    indexed.sort(key=lambda item: item.rel.lower())
    skipped.sort(key=str.lower)
    return indexed, skipped


def make_tree(files: list[IndexedFile]) -> str:
    if not files:
        return "_No files indexed._\n"
    return "\n".join(f"- `{item.rel}` ({item.size} bytes)" for item in files) + "\n"


def language_for(path: Path) -> str:
    if path.name == "Dockerfile":
        return "dockerfile"
    if path.name == ".gitignore":
        return "gitignore"
    return LANG_BY_EXT.get(path.suffix.lower(), "text")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").rstrip()


def build_markdown(
    root: Path,
    files: list[IndexedFile],
    skipped: list[str],
    tree_only: bool,
    max_file_kb: int,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        "# aemr-bot repository index",
        "",
        f"Generated at: `{generated_at}`",
        f"Root: `{root}`",
        f"Indexed files: `{len(files)}`",
        f"Max file size: `{max_file_kb} KB`",
        "",
        "## Safety policy",
        "",
        "The index excludes runtime secrets, `.env` files, logs, databases, dumps, archives, PDFs, images, virtual environments, caches and binary files.",
        "The committed template `.env.example` is allowed because it should not contain live credentials.",
        "",
        "## File tree",
        "",
        make_tree(files),
        "",
        "## Skipped files",
        "",
        "The following files were skipped intentionally:",
        "",
        *(skipped[:500] or ["_No skipped files recorded._"]),
    ]

    if len(skipped) > 500:
        lines.append(f"\n_Additional skipped entries omitted: {len(skipped) - 500}._")

    if tree_only:
        return "\n".join(lines).rstrip() + "\n"

    lines.extend(["", "## File contents", ""])
    for item in files:
        lang = language_for(item.path)
        lines.extend(
            [
                f"### `{item.rel}`",
                "",
                f"Size: `{item.size}` bytes  ",
                f"SHA-256: `{item.sha256}`",
                "",
                f"```{lang}",
                read_text(item.path),
                "```",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output_path = (root / args.output).resolve()
    max_file_bytes = args.max_file_kb * 1024

    if not root.exists():
        raise SystemExit(f"Root does not exist: {root}")
    if not root.is_dir():
        raise SystemExit(f"Root is not a directory: {root}")

    files, skipped = iter_files(root, max_file_bytes, output_path)
    markdown = build_markdown(root, files, skipped, args.tree_only, args.max_file_kb)
    output_path.write_text(markdown, encoding="utf-8", newline="\n")

    print(f"Indexed files: {len(files)}")
    print(f"Skipped files: {len(skipped)}")
    print(f"Written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
