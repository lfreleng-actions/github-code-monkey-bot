# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Symlinks in a proposal's base tree that stand for protected paths.

``policy.protected`` judges a path by its name, but a protected name
the base tree holds as a symlink means whatever its target holds, so
a proposal that edits the target changes the protected file without
touching its path. The check lists the base tree's symlinks once,
resolves those that stand for something protected, and refuses a
change to what they point at (DESIGN.md section 8).
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import proposal_policy as policy
from git_bounded import git

# A chain of base-tree symlinks longer than this is refused, as a loop
# is; a link reached through more directory links than this protects
# its whole target.
MAX_LINK_CHAIN = 8
# Names under which protected links are scheduled for resolution, per
# base tree; a tree whose links alias each other past this is refused
# outright.
MAX_LINK_CONTEXTS = 4096
# A recursive listing of a large repository's base tree, path and
# object name per file, fits far inside this.
TREE_LISTING_LIMIT = 64 * 1024 * 1024
# A symlink target longer than PATH_MAX cannot be checked out.
LINK_TARGET_LIMIT = 4096


class LinkError(ValueError):
    """A base-tree symlink that does not resolve inside the repository.

    ``followed`` holds the links reached before resolution failed, so
    each can be pinned: one left editable could be replaced by a file
    or directory that the protected name would then mean. ``resolved``
    is the path a chain past the bound still reaches, which stays
    protected, or None when there is none.
    """

    def __init__(
        self, message: str, followed: Sequence[str] = (), resolved: str | None = None
    ) -> None:
        super().__init__(message)
        self.followed = list(followed)
        self.resolved = resolved


def guards_protected(path: str) -> bool:
    """Whether a symlink at ``path`` stands for something protected.

    True when the path is protected itself, or when protected paths
    lie under it: a protected directory such as ``.claude``, or
    ``.github``, which holds the zizmor configuration and the
    instruction trees.
    """
    if policy.protected(path) or policy.protected(f"{path}/x"):
        return True
    lowered = path.lower()
    return lowered == ".github" or any(
        name.startswith(f"{lowered}/") for name in policy.PROTECTED_PATHS
    )


def link_target_parts(link: str, target: str) -> list[str]:
    """Split a stored link target, refusing one git cannot place in the tree."""
    if not target or target.startswith("/"):
        raise LinkError(f"{link} has an absolute or empty target")
    return target.split("/")


def resolve_link(
    link: str,
    read_target: Callable[[str], str | None],
    climbed: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Resolve a base-tree symlink to the path it stands for.

    ``read_target`` returns the stored target of a path the base tree
    holds as a symlink, and None for any other path. A target is read
    from its link's directory, and each component that is itself a
    link is followed, as a checkout would. Returns the path, which
    the tree may lack, and the links followed in order. Raises
    ``LinkError`` for an absolute or empty target, a ``..`` above the
    root, or a chain of more than ``MAX_LINK_CHAIN`` links, carrying
    the links followed before the failure. A chain past the bound is
    still followed until it ends or loops, so the error carries the
    path it stands for unless it loops; one past
    ``MAX_LINK_CONTEXTS`` links raises ``policy.Rejection``.
    ``climbed``, when given, receives each directory a ``..`` steps
    out of, as ``walk_link`` records it.
    """
    followed = [link]
    try:
        resolved = walk_link(link, read_target, followed, climbed)
    except LinkError as exc:
        raise LinkError(str(exc), followed) from None
    if len(followed) > MAX_LINK_CHAIN:
        raise LinkError(
            f"it follows more than {MAX_LINK_CHAIN} links", followed, resolved
        )
    return resolved, followed


def walk_link(
    link: str,
    read_target: Callable[[str], str | None],
    followed: list[str],
    climbed: list[str] | None = None,
) -> str:
    """Resolve ``link`` for ``resolve_link``, appending each link it follows.

    A checkout resolves ``dir/..`` only while ``dir`` exists as a
    directory, so each directory a ``..`` steps out of is appended to
    ``climbed``.

    A link reached again is a loop only when nothing that was pending
    at its last expansion has been consumed since: the walk from a
    link depends on nothing else, so it would reach the link again
    forever. Every endless walk does that, and a link reached again
    after the walk moved on, as ``a -> .`` in ``a/a/settings``, is a
    finite hop.
    """
    parts = link.split("/")[:-1]
    pending = deque(link_target_parts(link, read_target(link) or ""))
    # Expansions whose pending suffix is still untouched, as (length
    # of that suffix, link), the lengths never decreasing.
    expanding: list[tuple[int, str]] = [(0, link)]
    active = Counter([link])
    while pending:
        part = pending.popleft()
        while expanding and expanding[-1][0] > len(pending):
            active[expanding.pop()[1]] -= 1
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise LinkError("it leaves the repository")
            if climbed is not None:
                climbed.append("/".join(parts))
            parts.pop()
            continue
        current = "/".join([*parts, part])
        target = read_target(current)
        if target is None:
            parts.append(part)
            continue
        if active[current]:
            raise LinkError("it loops")
        if len(followed) >= MAX_LINK_CONTEXTS:
            raise policy.Rejection(
                f"{link} follows more than {MAX_LINK_CONTEXTS} symlinks in the base tree"
            )
        followed.append(current)
        expanding.append((len(pending), current))
        active[current] += 1
        pending.extendleft(reversed(link_target_parts(current, target)))
    return "/".join(parts)


def first_link_path(link: str, target: str) -> str | None:
    """The path a link's own target names, before any further link is followed."""
    if not target or target.startswith("/"):
        return None
    parts = link.split("/")[:-1]
    for part in target.split("/"):
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts) or None


@dataclass
class LinkGuard:
    """Paths a proposal may not change because a protected link stands for them.

    ``pinned`` maps an exact path to its cause: a protected link
    itself, a link on its chain, or the first path of a link that
    cannot be resolved. ``targets`` holds each resolved target, the
    protected name it stands for, and the link that names the cause;
    a name of None protects everything under the target.
    ``directories`` maps a directory a resolved link climbs out of
    with ``..`` to its cause: removing or replacing it would leave
    the link dangling, and whether a change empties it is not known
    per path, so every path at or under it is refused.
    """

    pinned: dict[str, str] = field(default_factory=dict[str, str])
    targets: list[tuple[str, str | None, str]] = field(
        default_factory=list[tuple[str, str | None, str]]
    )
    directories: dict[str, str] = field(default_factory=dict[str, str])
    holders: dict[str, str] | None = field(default=None, init=False, repr=False)

    def cause(self, path: str) -> str | None:
        """Why ``path`` is protected through a link, or None when it is not.

        A path under a linked directory counts when the same path
        under the link would be protected, or would hold protected
        paths, so a link to ``.claude`` protects all of its target
        and a link to ``.github`` only the protected paths in it.
        A path that holds a protected one counts too: a diff names a
        submodule it deletes, not the paths inside it.
        """
        if path in self.pinned:
            return self.pinned[path]
        for target, seen_as, link in self.targets:
            if path == target:
                return f"target of {link}"
            under = beneath(path, target, seen_as or "")
            if under is not None and (seen_as is None or guards_protected(under)):
                return f"target of {link}"
        for directory, cause in self.directories.items():
            if path == directory or path.startswith(f"{directory}/"):
                return cause
        return self.holding().get(path)

    def holding(self) -> dict[str, str]:
        """Each proper ancestor of a protected path, mapped to its cause."""
        if self.holders is None:
            held = [(target, f"target of {link}") for target, _, link in self.targets]
            held += [*self.pinned.items(), *self.directories.items()]
            self.holders = {}
            for path, cause in held:
                parts = path.split("/")
                for depth in range(1, len(parts)):
                    ancestor = "/".join(parts[:depth])
                    self.holders.setdefault(ancestor, f"holds {path}, {cause}")
        return self.holders


def beneath(path: str, target: str, seen_as: str) -> str | None:
    """``path`` named through the link, when it lies under ``target``."""
    if not target:
        return f"{seen_as}/{path}"
    if path.startswith(f"{target}/"):
        return seen_as + path[len(target) :]
    return None


def link_guard(
    links: Iterable[str], read_target: Callable[[str], str | None]
) -> LinkGuard:
    """Resolve every base-tree symlink that stands for something protected.

    ``links`` names the base tree's symlinks and ``read_target``
    reads one's stored target, as in ``resolve_link``. A link inside
    the target of a protected directory link counts under the name
    the directory link gives it, so ``.claude/settings.json`` reached
    through two links is protected like one reached through one. A
    link reached under several names counts under each, unless one
    already protects everything beneath it. One reached through more
    than ``MAX_LINK_CHAIN`` directory links protects its whole
    target, and so does every link inside that, so neither bound
    leaves a protected path editable. Raises ``policy.Rejection``
    when the links alias each other under more than
    ``MAX_LINK_CONTEXTS`` names, before scheduling more.
    """
    names = sorted(links)
    queue: deque[tuple[str, str | None, str, int]] = deque()
    scheduled: set[tuple[str, str | None]] = set()
    covered: set[str] = set()
    resolutions: dict[str, tuple[str, list[str]] | LinkError] = {}
    climbs: dict[str, list[str]] = {}

    def schedule(path: str, seen_as: str | None, cause: str, depth: int) -> None:
        """Queue ``path`` under ``seen_as`` once, within the context cap."""
        if depth > MAX_LINK_CHAIN:
            seen_as = None
        if (path, seen_as) in scheduled or path in covered:
            return
        if len(scheduled) >= MAX_LINK_CONTEXTS:
            raise policy.Rejection(
                f"the base tree's protected symlinks alias each other under "
                f"more than {MAX_LINK_CONTEXTS} names"
            )
        scheduled.add((path, seen_as))
        queue.append((path, seen_as, cause, depth))

    for link in names:
        if guards_protected(link):
            schedule(link, link, link, 1)
    guard = LinkGuard()
    while queue:
        path, seen_as, cause, depth = queue.popleft()
        if path in covered:
            continue
        if seen_as is None or covers_all(seen_as):
            covered.add(path)
        if path not in resolutions:
            climbs[path] = []
            try:
                resolutions[path] = resolve_link(path, read_target, climbs[path])
            except LinkError as exc:
                resolutions[path] = exc
        result = resolutions[path]
        if isinstance(result, LinkError):
            pin_unresolved(guard, path, read_target, result)
            if result.resolved is None:
                continue
            result = result.resolved, result.followed
        resolved, followed = result
        own = f"{path} is a protected link" if path == cause else f"target of {cause}"
        guard.pinned.setdefault(path, own)
        for hop in followed[1:]:
            guard.pinned.setdefault(hop, f"target of {cause}")
        for directory in needed_directories(climbs[path], followed):
            passes = f"{cause} passes through {directory}"
            guard.directories.setdefault(directory, passes)
        guard.targets.append((resolved, seen_as, cause))
        for other in links_under(names, resolved):
            under = beneath(other, resolved, seen_as or "")
            if under is not None and (seen_as is None or guards_protected(under)):
                schedule(other, under if seen_as else None, cause, depth + 1)
    return guard


def needed_directories(climbed: Iterable[str], followed: Sequence[str]) -> list[str]:
    """The directories in ``climbed`` that no link in ``followed`` lies under.

    A directory holding a pinned link stays while the link does, so
    only the others need protecting.
    """
    held = {
        "/".join(hop.split("/")[:depth])
        for hop in followed
        for depth in range(1, hop.count("/") + 1)
    }
    return sorted({path for path in climbed if path not in held})


def links_under(names: list[str], target: str) -> list[str]:
    """The sorted ``names`` that lie under ``target``, or all for the root."""
    if not target:
        return names
    prefix = f"{target}/"
    start = bisect_left(names, prefix)
    end = start
    while end < len(names) and names[end].startswith(prefix):
        end += 1
    return names[start:end]


def covers_all(seen_as: str) -> bool:
    """Whether every path under ``seen_as`` is protected by its name alone."""
    return policy.protected(f"{seen_as}/x")


def pin_unresolved(
    guard: LinkGuard,
    path: str,
    read_target: Callable[[str], str | None],
    exc: LinkError,
) -> None:
    """Pin an unresolvable link, each link it reached, and each one's first path."""
    reason = f"{path} -> {read_target(path) or ''} cannot be resolved: {exc}"
    for hop in exc.followed or [path]:
        guard.pinned.setdefault(hop, reason)
        first = first_link_path(hop, read_target(hop) or "")
        if first is not None:
            guard.pinned.setdefault(first, reason)


def base_links(clone: Path, base_sha: str) -> LinkGuard:
    """The base tree's protected symlinks, resolved, from one tree listing.

    A target is read only when resolving a protected link reaches it.
    """
    listing = cast(
        "bytes",
        git(
            clone,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            base_sha,
            binary=True,
            limit=TREE_LISTING_LIMIT,
        ),
    )
    # surrogateescape keeps distinct byte paths distinct; "replace"
    # would merge two links whose names differ in invalid bytes.
    blobs: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) == 3 and fields[0] == policy.MODE_SYMLINK:
            blobs[name.decode("utf-8", "surrogateescape")] = fields[2]
    targets: dict[str, str] = {}

    def read_target(path: str) -> str | None:
        """The stored target of a base-tree symlink, or None for any other path."""
        if path not in blobs:
            return None
        if path not in targets:
            stored = git(
                clone,
                "cat-file",
                "blob",
                blobs[path],
                binary=True,
                limit=LINK_TARGET_LIMIT,
            )
            content = cast("bytes", stored)
            # A POSIX checkout passes the target to symlink(2) as a C
            # string, so a NUL would cut it short of the path recorded.
            if b"\0" in content:
                raise policy.Rejection(
                    f"{path} in the base tree is a symlink whose target holds a NUL byte"
                )
            targets[path] = content.decode("utf-8", "surrogateescape")
        return targets[path]

    return link_guard(blobs, read_target)
