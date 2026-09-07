"""Server-sent events, with a heartbeat and a watchdog.

A resolver run is minutes of silence punctuated by bursts. Without a heartbeat
that silence is indistinguishable from a dead connection, and every proxy
between here and the browser will eventually decide it is the latter. So the
stream emits a comment frame on a fixed interval whether or not anything has
happened.

The watchdog is the other half of the same problem, and the obvious version of
it is wrong. Timing out on *silence* disconnects a healthy client whenever a run
goes quiet, which on a large model is most of it. And a client that has genuinely
stopped reading does not show up as silence at all: it blocks the generator at
``yield``, so the idle branch is never even reached.

What a stalled consumer actually produces is dropped frames -- the queue fills,
the producer discards the oldest, and the stream stops being a faithful record of
the run. So that is what the watchdog watches. Once frames have been lost the
stream closes and says so, and the client reconnects and refetches state rather
than quietly acting on a history with holes in it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0
WATCHDOG_SECONDS = 120.0
MAX_QUEUE = 256


@dataclass
class Subscriber:
    topic: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    last_drained: float = field(default_factory=time.monotonic)
    dropped: int = 0


class EventBus:
    """In-process fan-out. One instance per worker; topics are model or zone ids."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, topic: str) -> Subscriber:
        sub = Subscriber(topic=topic)
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, topic: str, event: str, data: dict[str, Any]) -> int:
        """Push to every subscriber on ``topic``. Never blocks the producer.

        A full queue drops the oldest frame and counts it. Dropping is better
        than blocking: the producer here is the pipeline itself, and a slow
        browser must not be able to stall a resolver run.
        """
        payload = {"event": event, "data": data, "ts": time.time()}
        delivered = 0
        for sub in list(self._subscribers):
            if sub.topic not in (topic, "*"):
                continue
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()
                    sub.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                sub.queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:  # pragma: no cover - drained just above
                sub.dropped += 1
        return delivered


BUS = EventBus()


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream(
    topic: str,
    bus: EventBus | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
    watchdog: float = WATCHDOG_SECONDS,
) -> AsyncIterator[str]:
    bus = bus or BUS
    sub = bus.subscribe(topic)
    first_drop: float | None = None
    try:
        yield format_sse("open", {"topic": topic, "heartbeat_s": heartbeat})
        while True:
            if sub.dropped:
                now = time.monotonic()
                first_drop = first_drop if first_drop is not None else now
                if now - first_drop >= watchdog:
                    yield format_sse(
                        "closing",
                        {
                            "reason": "watchdog",
                            "detail": "consumer fell behind and frames were dropped",
                            "dropped": sub.dropped,
                        },
                    )
                    return
            try:
                payload = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except TimeoutError:
                # A comment frame: keeps proxies from reaping an idle stream and
                # costs a client nothing to ignore. Silence is not a fault.
                yield ": heartbeat\n\n"
                continue
            sub.last_drained = time.monotonic()
            yield format_sse(payload["event"], {**payload["data"], "ts": payload["ts"]})
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(sub)
