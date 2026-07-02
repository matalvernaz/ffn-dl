"""Tests for the shared audio engine + live-TTS sequencing.

The OpenAL backend needs a real device and isn't exercised here; these tests
drive the backend-agnostic engine logic with a fake backend, and the live-TTS
controller with a fake engine + fake synth.
"""
from ficary.audio.engine import (
    CHANNEL_VOICE,
    AudioEngine,
    ramp_values,
)
from ficary.audio.events import Event, ReaderEvent
from ficary.reader.live_tts import LiveTTSController


class FakeBackend:
    available = True

    def __init__(self):
        self.gains = {}
        self.played = []
        self._next = 1
        self._playing = set()

    def load(self, path, *, looping):
        h = self._next
        self._next += 1
        return h

    def play(self, h):
        self.played.append(h)
        self._playing.add(h)

    def pause(self, h):
        self._playing.discard(h)

    def stop(self, h):
        self._playing.discard(h)

    def set_gain(self, h, gain):
        self.gains[h] = gain

    def set_position(self, h, az, el, dist):
        pass

    def is_playing(self, h):
        return h in self._playing

    def set_reverb(self, size):
        pass

    def shutdown(self):
        pass


def test_ramp_values_ends_exactly():
    vals = ramp_values(0.0, 1.0, 4)
    assert vals == [0.25, 0.5, 0.75, 1.0]
    assert ramp_values(1.0, 0.0, 2) == [0.5, 0.0]


def test_event_bus_fanout_and_isolation():
    engine = AudioEngine(backend=FakeBackend())
    seen_a, seen_b = [], []
    engine.subscribe(lambda e: seen_a.append(e.kind))

    def bad(e):
        raise RuntimeError("boom")

    engine.subscribe(bad)  # must not break others
    engine.subscribe(lambda e: seen_b.append(e.kind))
    engine.emit(Event(ReaderEvent.TTS_STARTED))
    assert seen_a == [ReaderEvent.TTS_STARTED]
    assert seen_b == [ReaderEvent.TTS_STARTED]


def test_play_file_sets_channel_gain_and_plays():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    engine.set_gain(CHANNEL_VOICE, 0.8)
    h = engine.play_file("x.mp3", CHANNEL_VOICE)
    assert h in be.played
    assert be.gains[h] == 0.8


def test_duck_immediate_lowers_channel_gain():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    h = engine.add_looping_source("amb.ogg", gain=1.0)
    engine.duck("ambient", to=0.25, ms=0)
    assert be.gains[h] == 0.25
    engine.set_gain("ambient", 1.0)
    assert be.gains[h] == 1.0


class FakeEngine:
    """Minimal engine surface LiveTTSController uses; on_done fires at once."""

    def __init__(self):
        self.events = []
        self.played = []

    def play_file(self, path, channel, on_done=None):
        self.played.append(path)
        if on_done:
            on_done()

    def pause(self, channel):
        pass

    def resume(self, channel):
        pass

    def stop(self, channel):
        pass

    def emit(self, event):
        self.events.append(event.kind)


def test_live_tts_walks_all_chunks(tmp_path):
    engine = FakeEngine()
    highlights = []

    def fake_synth(voice, text, out, *, rate=None):
        out.write_bytes(b"\x00")

    ctrl = LiveTTSController(
        engine, voice="edge:test", on_highlight=lambda c: highlights.append(c.index),
        synth=fake_synth, tmp_dir=tmp_path,
    )
    ctrl.start("First sentence here.\n\nSecond paragraph now.", chapter_number=3)
    ctrl._worker.join(timeout=5.0)

    assert ReaderEvent.TTS_STARTED in engine.events
    assert ReaderEvent.TTS_STOPPED in engine.events
    assert highlights == [0, 1]  # two paragraphs → two chunks, in order
    assert len(engine.played) == 2


def _wait_for(cond, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_pause_does_not_complete_the_chunk():
    """A paused source reports not-playing; the poller must not mistake
    that for completion (it used to fire on_done within one poll tick of
    pause, so resume() had nothing left to resume)."""
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    done = []
    h = engine.play_file("x.mp3", CHANNEL_VOICE, on_done=lambda: done.append(1))
    engine.pause(CHANNEL_VOICE)
    import time
    time.sleep(0.35)  # several poll ticks
    assert done == []
    assert h in engine._channels[CHANNEL_VOICE].handles
    engine.resume(CHANNEL_VOICE)
    be.stop(h)  # simulate the clip actually finishing
    assert _wait_for(lambda: done == [1])


def test_finished_clip_releases_backend_source():
    be = FakeBackend()
    be.stopped = []
    original_stop = be.stop
    be.stop = lambda h: (be.stopped.append(h), original_stop(h))[1]
    engine = AudioEngine(backend=be)
    done = []
    h = engine.play_file("x.mp3", CHANNEL_VOICE, on_done=lambda: done.append(1))
    be._playing.discard(h)  # clip finishes on its own
    assert _wait_for(lambda: done == [1])
    assert h in be.stopped  # poller released the source (leak fix)


def test_channel_fade_preserves_per_sound_mix():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    rain = engine.add_looping_source("rain.ogg", gain=0.6, channel="ambient")
    fire = engine.add_looping_source("fire.ogg", gain=0.4, channel="ambient")
    engine.duck("ambient", to=0.5, ms=0)
    assert abs(be.gains[rain] - 0.3) < 1e-9
    assert abs(be.gains[fire] - 0.2) < 1e-9
    engine.restore("ambient", ms=0)  # target still 1.0
    assert abs(be.gains[rain] - 0.6) < 1e-9
    assert abs(be.gains[fire] - 0.4) < 1e-9


def test_set_gain_updates_restore_target():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    h = engine.add_looping_source("amb.ogg", gain=1.0, channel="ambient")
    engine.set_gain("ambient", 0.7)
    engine.duck("ambient", to=0.25, ms=0)
    engine.restore("ambient", ms=0)
    assert abs(be.gains[h] - 0.7) < 1e-9  # restore returns to the SET level


def test_play_on_paused_channel_starts_paused():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    engine.play_file("a.mp3", CHANNEL_VOICE)
    engine.pause(CHANNEL_VOICE)
    h2 = engine.play_file("b.mp3", CHANNEL_VOICE)
    assert h2 is not None
    assert not be.is_playing(h2)  # registered paused, not audibly playing
    engine.resume(CHANNEL_VOICE)
    assert be.is_playing(h2)


class RecordingEngine(FakeEngine):
    """FakeEngine that also keeps the full Event objects."""

    def __init__(self):
        super().__init__()
        self.raw = []

    def emit(self, event):
        self.raw.append(event)
        super().emit(event)


def test_live_tts_stop_during_slow_synth_plays_nothing(tmp_path):
    import threading as _t
    release = _t.Event()

    def slow_synth(voice, text, out, *, rate=None):
        release.wait(3.0)
        out.write_bytes(b"\x00")

    engine = RecordingEngine()
    ctrl = LiveTTSController(engine, voice="edge:test", synth=slow_synth,
                             tmp_dir=tmp_path)
    ctrl.start("Only paragraph.", chapter_number=1)
    import time
    time.sleep(0.1)  # worker is now inside the synth
    ctrl.stop()
    release.set()
    time.sleep(0.3)  # give the straggler time to (incorrectly) play
    assert engine.played == []  # the superseded generation never plays
    stops = [e for e in engine.raw if e.kind == ReaderEvent.TTS_STOPPED]
    assert stops and stops[-1].payload.get("reason") == "stopped"


def test_live_tts_no_double_synth(tmp_path):
    import threading as _t
    lock = _t.Lock()
    calls = []

    def counting_synth(voice, text, out, *, rate=None):
        with lock:
            calls.append(text)
        import time
        time.sleep(0.05)
        out.write_bytes(b"\x00")

    engine = FakeEngine()
    ctrl = LiveTTSController(engine, voice="edge:test", synth=counting_synth,
                             tmp_dir=tmp_path)
    ctrl.start("One here.\n\nTwo here.\n\nThree here.", chapter_number=1)
    ctrl._worker.join(timeout=5.0)
    assert sorted(calls) == sorted(set(calls))  # each chunk synthed once


def test_live_tts_failed_chunk_retries_then_reports(tmp_path):
    attempts = {}

    def flaky_synth(voice, text, out, *, rate=None):
        attempts[text] = attempts.get(text, 0) + 1
        if "Second" in text:
            raise RuntimeError("synth down")
        out.write_bytes(b"\x00")

    engine = RecordingEngine()
    completions = []
    ctrl = LiveTTSController(
        engine, voice="edge:test", synth=flaky_synth, tmp_dir=tmp_path,
        on_complete=lambda failed: completions.append(failed),
    )
    ctrl.start("First bit.\n\nSecond bit.\n\nThird bit.", chapter_number=1)
    ctrl._worker.join(timeout=5.0)
    assert len(engine.played) == 2  # first + third
    assert attempts["Second bit."] == 2  # one retry before giving up
    assert completions == [1]
    finals = [e for e in engine.raw if e.kind == ReaderEvent.TTS_STOPPED]
    assert finals[-1].payload.get("reason") == "completed"
    assert finals[-1].payload.get("failed_chunks") == 1


def test_pause_resume_inactive_controller_is_silent(tmp_path):
    engine = RecordingEngine()
    ctrl = LiveTTSController(engine, voice="edge:test",
                             synth=lambda v, t, o, *, rate=None: o.write_bytes(b"\x00"),
                             tmp_dir=tmp_path)
    ctrl.pause()
    ctrl.resume()
    assert engine.raw == []  # no bus noise from an idle controller


def test_to_edge_rate_normalization():
    from ficary.reader.live_tts import _to_edge_rate
    assert _to_edge_rate("0") is None
    assert _to_edge_rate("") is None
    assert _to_edge_rate(None) is None
    assert _to_edge_rate("10") == "+10%"
    assert _to_edge_rate("-5") == "-5%"
    assert _to_edge_rate("+15%") == "+15%"
    assert _to_edge_rate("fast") is None
