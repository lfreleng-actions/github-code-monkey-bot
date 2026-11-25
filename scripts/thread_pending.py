# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which resolved threads only the watermark hears, and the word a round owes.

A resolved thread the bot never replied on holds a maintainer's word
the selection hears only while it is newer than the round's watermark.
A round that carries such a thread and posts no reply on it, left out
by the agent or refused by GitHub, records the oldest such word as its
footer's ``pending``, and the next selection hears the thread again
(DESIGN.md section 20).
"""

from __future__ import annotations

from typing import Any


def watermark_only(thread: dict[str, Any]) -> bool:
    """Whether only the watermark brings back a resolved thread's maintainer word.

    The bot never replied on the thread, so once a round's footer moves
    the watermark past that word, nothing hears it again. A thread whose
    word no reply of the bot's answers is heard as unanswered; one the
    bot answered owes nothing, so neither is held nor overflows.
    """
    return (
        thread.get("resolved") is True
        and thread.get("maintainer_unanswered") is not True
        and thread.get("maintainer_answered") is not True
    )


def held(thread: dict[str, Any], pending: str | None) -> bool:
    """Whether an earlier round carried this thread's word and left it unanswered."""
    word = held_word(thread)
    return pending is not None and word is not None and word >= pending


def held_word(thread: dict[str, Any]) -> str | None:
    """The maintainer's word only the watermark hears on a thread, or None."""
    latest = thread.get("maintainer_latest")
    return latest if watermark_only(thread) and isinstance(latest, str) else None


def pending_since(threads: list[dict[str, Any]], answered: set[str]) -> str | None:
    """The oldest ``held`` word on a carried thread no reply answered, or None.

    The round's footer records it as ``pending``, so the word outlives a
    reply the agent left out or the publisher failed to post.
    """
    words = [
        str(t["held"])
        for t in threads
        if isinstance(t.get("held"), str) and t["thread_id"] not in answered
    ]
    return min(words, default=None)
