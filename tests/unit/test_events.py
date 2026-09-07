"""SSE: heartbeat, watchdog, and never blocking the pipeline."""
from __future__ import annotations

import pytest

from app.api.events import MAX_QUEUE, EventBus, format_sse, stream


def test_a_subscriber_only_receives_its_own_topic():
    bus = EventBus()
    mine = bus.subscribe("model-1")
    theirs = bus.subscribe("model-2")

    bus.publish("model-1", "zone.resolved", {"zone": "a"})

    assert mine.queue.qsize() == 1
    assert theirs.queue.qsize() == 0


def test_a_wildcard_subscriber_receives_everything():
    bus = EventBus()
    watcher = bus.subscribe("*")
    bus.publish("model-1", "x", {})
    bus.publish("model-2", "y", {})
    assert watcher.queue.qsize() == 2


def test_a_full_queue_drops_the_oldest_frame_and_never_blocks():
    """The producer is the pipeline. A stalled browser must not stall a run."""
    bus = EventBus()
    sub = bus.subscribe("t")
    for i in range(MAX_QUEUE + 10):
        bus.publish("t", "tick", {"i": i})

    assert sub.queue.qsize() == MAX_QUEUE
    assert sub.dropped == 10
    first = sub.queue.get_nowait()
    assert first["data"]["i"] == 10, "the oldest frames should have been dropped, not the newest"


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe("t")
    bus.unsubscribe(sub)
    assert bus.publish("t", "x", {}) == 0
    assert bus.subscriber_count == 0


def test_sse_framing_is_well_formed():
    frame = format_sse("zone.resolved", {"zone": "L0|0_0", "verified": 3})
    assert frame.startswith("event: zone.resolved\n")
    assert "data: " in frame
    assert frame.endswith("\n\n"), "an SSE frame must end with a blank line"


@pytest.mark.asyncio
async def test_the_stream_emits_a_heartbeat_when_nothing_happens():
    bus = EventBus()
    agen = stream("t", bus=bus, heartbeat=0.01, watchdog=100.0)

    opening = await agen.asend(None)
    assert "event: open" in opening

    beat = await agen.asend(None)
    assert beat.startswith(": "), "an idle stream must emit a comment frame to hold the connection"
    await agen.aclose()


@pytest.mark.asyncio
async def test_the_stream_delivers_a_published_event():
    bus = EventBus()
    agen = stream("t", bus=bus, heartbeat=5.0, watchdog=100.0)
    await agen.asend(None)

    bus.publish("t", "zone.resolved", {"zone": "L0|0_0"})
    frame = await agen.asend(None)

    assert "event: zone.resolved" in frame
    assert "L0|0_0" in frame
    await agen.aclose()


@pytest.mark.asyncio
async def test_a_quiet_run_is_not_treated_as_a_dead_client():
    """Silence is normal. A large model is mostly silence."""
    bus = EventBus()
    agen = stream("t", bus=bus, heartbeat=0.01, watchdog=0.0)
    await agen.asend(None)

    for _ in range(3):
        frame = await agen.asend(None)
        assert frame.startswith(": "), f"a quiet stream was closed: {frame!r}"
    await agen.aclose()


@pytest.mark.asyncio
async def test_the_watchdog_closes_a_stream_that_has_lost_frames():
    """A consumer that fell behind gets told, rather than served a history with holes."""
    bus = EventBus()
    agen = stream("t", bus=bus, heartbeat=0.01, watchdog=0.0)
    await agen.asend(None)

    for i in range(MAX_QUEUE + 5):
        bus.publish("t", "tick", {"i": i})

    closing = await agen.asend(None)
    assert "event: closing" in closing
    assert "watchdog" in closing
    assert "dropped" in closing

    with pytest.raises(StopAsyncIteration):
        await agen.asend(None)


@pytest.mark.asyncio
async def test_closing_the_stream_unsubscribes():
    bus = EventBus()
    agen = stream("t", bus=bus, heartbeat=5.0, watchdog=100.0)
    await agen.asend(None)
    assert bus.subscriber_count == 1
    await agen.aclose()
    assert bus.subscriber_count == 0, "a closed stream must not leave a queue behind"


def test_publish_is_safe_with_no_subscribers():
    bus = EventBus()
    assert bus.publish("nobody-listening", "x", {"a": 1}) == 0


@pytest.mark.asyncio
async def test_the_pipeline_publishes_progress(db, project, settings, store):
    """The events a reviewer's browser actually needs to see."""
    from app.api.events import BUS
    from app.pipeline import run_pipeline
    from tests.conftest import GENERATED, make_model_version

    mv = make_model_version(db, project, GENERATED / "gravity_trap.ifc")
    sub = BUS.subscribe(mv.id)
    try:
        run_pipeline(db, mv, store=store, top_n=10)
        db.commit()
        seen = []
        while not sub.queue.empty():
            seen.append(sub.queue.get_nowait()["event"])
    finally:
        BUS.unsubscribe(sub)

    for expected in ("ingest.started", "ingest.complete", "dispatch", "run.complete"):
        assert expected in seen, f"{expected} was never published; saw {seen}"
    assert "zone.resolved" in seen


def _ensure_asyncio_marker():
    """pytest-asyncio must be installed, or every async test above silently skips."""
    import importlib.util

    assert importlib.util.find_spec("pytest_asyncio") is not None, (
        "pytest-asyncio is missing; the async SSE tests would be skipped rather "
        "than run, and a skipped test proves nothing"
    )


def test_async_tests_are_actually_running():
    _ensure_asyncio_marker()
