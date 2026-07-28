"""Regression coverage for the public Markdown documentation graph."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")
DELETED_RUNBOOK = "RUNBOOK-e2e-demo.md"


def _local_target(link: str) -> str | None:
    target = link.strip().strip("<>").split(maxsplit=1)[0]
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith(("#", "/")):
        return None
    return unquote(parsed.path)


def test_markdown_local_links_resolve_and_use_the_canonical_demo_runbook() -> None:
    documents = [ROOT / "README.md", ROOT / "KB.md", *sorted((ROOT / "docs").rglob("*.md"))]
    contents = "\n".join(document.read_text(encoding="utf-8") for document in documents)

    assert DELETED_RUNBOOK not in contents
    assert "docs/demo-db-incident-e2e.md" in (ROOT / "README.md").read_text(encoding="utf-8")

    broken: list[str] = []
    for document in documents:
        for raw_link in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = _local_target(raw_link)
            if target and not (document.parent / target).resolve().exists():
                broken.append(f"{document.relative_to(ROOT)} -> {raw_link}")
    assert not broken, "Broken local Markdown links:\n" + "\n".join(broken)
