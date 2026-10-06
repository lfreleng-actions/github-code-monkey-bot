# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Issue categories a run can opt into.

Each category maps to the label vocabulary ``github-issues-triage``
applies, with the issue Type as a second signal for bugs and
features. An issue belongs to every category it matches; one that
matches none belongs to ``other``. A run works an issue when any of
its categories is enabled.
"""

from __future__ import annotations

import re

# Category name -> (triage labels, issue Types) that place an issue in it.
CATEGORIES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "bugs": (frozenset({"bug"}), frozenset({"Bug"})),
    "features": (frozenset({"feature"}), frozenset({"Feature"})),
    "docs": (frozenset({"documentation"}), frozenset()),
    "ci": (frozenset({"CI"}), frozenset()),
    "code_quality": (frozenset({"code-quality"}), frozenset()),
    "refactor": (frozenset({"refactor"}), frozenset()),
    "performance": (frozenset({"performance"}), frozenset()),
}
OTHER = "other"
ALL_CATEGORIES = (*CATEGORIES, OTHER)


class CategoryError(ValueError):
    """A category list that names nothing, or names something unknown."""


def parse_categories(text: str) -> frozenset[str]:
    """Read ``all`` or a comma/space list of category names."""
    tokens = [t for t in re.split(r"[,\s]+", text.strip().lower()) if t]
    if not tokens:
        raise CategoryError("no issue category selected; enable at least one")
    if tokens == ["all"]:
        return frozenset(ALL_CATEGORIES)
    unknown = sorted(set(tokens) - set(ALL_CATEGORIES))
    if unknown:
        raise CategoryError(
            f"unknown categories {', '.join(unknown)}; "
            f"choose from {', '.join(ALL_CATEGORIES)}"
        )
    return frozenset(tokens)


def categories_of(labels: list[str], issue_type: str | None) -> frozenset[str]:
    """Every category an issue belongs to; ``other`` when it matches none."""
    names = set(labels)
    found = {
        name
        for name, (category_labels, types) in CATEGORIES.items()
        if names & category_labels or issue_type in types
    }
    return frozenset(found or {OTHER})
