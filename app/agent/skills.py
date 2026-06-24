"""Skill loading: markdown guidance injected into the agent's system prompt.

A "skill" is a markdown file with YAML frontmatter (name/description) plus a
body of guidance. The body is prepended as a system message so the model
follows it. Currently the search-research skill is loaded whenever the
web_search tool is enabled.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _load_body(filename: str) -> str:
    """Read a skill file and strip its YAML frontmatter, returning the body."""
    path = SKILLS_DIR / filename
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER.sub("", text, count=1).strip()


@lru_cache
def search_skill() -> str:
    return _load_body("web-search-researcher.md")


@lru_cache
def deep_research_skill() -> str:
    return _load_body("deep-research.md")
