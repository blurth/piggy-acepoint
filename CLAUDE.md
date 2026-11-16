# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AcePoint (羽毛球手势计分系统) — a self-contained badminton scoreboard: Raspberry Pi 4 + two back-to-back USB cameras + a MAX7219 8×32 LED matrix, powered by a USB power bank on the net post. Players score by holding a Victory gesture at the net; the half-court they face is incremented. No referee, no touch, no mains power, no network.

`羽毛球手势计分系统-需求文档.md` (Chinese) is the spec of record — hardware BOM, wiring, power budget, anti-false-trigger rules, tunable parameters (§7), acceptance criteria (§9), staged roadmap (§10). Read it before changing behavior.

**Current stage: development/integration.** Cameras are stubbed by recorded video files and the LED matrix by a web scoreboard. Both are pluggable backends — moving to the real device is a config swap (`config.pi.toml`), not a code change. Do not hard-code either one back into the pipeline.

`legacy/badminton_pi4.py` is the pre-refactor single-file prototype, kept only for reference. Never edit it; everything it did now lives in `src/acepoint/`.

## Commands

```powershell
# Windows dev machine
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts/fetch_model.py               # gesture_recognizer.task is NOT in the repo

acepoint                                    # config.toml: videos + web scoreboard on :8080
acepoint --check                            # env self-check (model / sources / LED deps)
acepoint --display console --duration 20    # headless smoke test, exits by itself
acepoint --video left=videos/A.mp4 --video right=videos/B.mp4
python scripts/make_sample_videos.py        # placeholder clips when real footage is missing

pytest
pytest tests/test_fsm.py::test_cooldown_blocks_everything
```

Tuning is measured, not guessed. `scripts/` holds four analysis tools that import the project's own `crop_roi`/`GestureFSM`/config so their numbers describe what actually runs:

```powershell
python scripts/analyze_gestures.py videos/B.mp4 --sweep   # hold_seconds / hold_grace calibration
python scripts/compare_roi.py videos/B.mp4                # ROI crop ratios vs detection
python scripts/compare_recognizer.py videos/B.mp4         # num_hands, per_source_recognizer
python scripts/profile_pipeline.py --resolution           # stage timings, RSS, resolution curve
python scripts/check_sampling.py                          # phase sweep — mandatory after touching poll_hz or any hold_* value
python scripts/explain_moment.py videos/B.mp4 14          # why a specific gesture did not score
python scripts/record_camera.py -c config.pi.toml left    # record calibration footage from a configured source (on the device)
```

Before treating "the gesture didn't register" as a recognition bug, run `explain_moment.py` — on rehearsal footage where gestures come back to back, the usual cause is the per-side cooldown, not detection. `SideStatus.detected` (recognizer output) is deliberately separate from `SideStatus.gesture` (what the FSM is accumulating) so the dashboard can tell those apart; the FSM clears `gesture` during cooldown.

Every parameter in `[scoring]`, `[capture]`, and `[recognition]` was set from these. Re-run them before changing any of those values, and again when the footage changes — the current numbers come from handheld portrait clips, not the net-post landscape framing the real device will see.

```bash
# Raspberry Pi
pip install -e ".[pi]"                      # adds luma.led-matrix; SPI must be enabled first
acepoint -c config.pi.toml
bash deploy/pi_update.sh                    # on the device: pull, copy into ~/AcePoint, patch config keys, --check, restart
curl -s localhost:8080/api/state            # per-side fps and recognize_ms — the numbers that say whether the Pi keeps up
```

The device runs with the overlay file system on (`overlayroot=tmpfs`, boot partition read-only) because it has no power switch and is unplugged hot. Anything written there — an update, a config edit, calibration footage — is gone at the next reboot; `pi_update.sh` warns when it sees the overlay. To make a change stick: `sudo raspi-config nonint disable_overlayfs` → reboot → change → `enable_overlayfs` → reboot (deploy/README.md §7).

The Pi 4 cannot run mediapipe 1.x: its aarch64 wheel needs ARMv8 AES, which the BCM2711 lacks, so the process dies with SIGILL at startup. The device runs Python 3.12 (via uv) + mediapipe 0.10.18 — `deploy/pi4_mediapipe_fix.sh`. The dev machine is on 1.0.0; keep `recognition.py` on the `tasks` API that both versions share.

Tuning parameters live in `config.toml` / `config.pi.toml` (spec §7) — never re-introduce module-level constants for them.

## Architecture

```
cli.py  →  Config (TOML)  →  ScoringEngine
                               │
   sources/  camera.py | video.py ──┐
                                    ├─ crop_roi → GestureReader → GestureFSM ×2 → ScoreBoard
   displays/ web.py | led.py | console.py ←── Telemetry (10 Hz)
```

By default recognition runs single-threaded in the main loop; every display backend owns its own thread. Left and right take turns holding a `1/poll_hz` time slice, so each side gets `poll_hz/2` fps (spec §6). With `recognition.parallel` (on in `config.pi.toml`) each side's read + recognition runs in its own thread and one beat lasts `2/poll_hz` — same per-side rate, no queueing behind the other side.

Things that are easy to get wrong:

- **Alternating recognition cannot hold the calibrated 5 fps per side on the Pi 4 — that is what `recognition.parallel` and `capture.decode_on_demand` are for.** MediaPipe inference is single-threaded on Linux; measured on the device it takes 157–219 ms per frame (21 ms on the dev machine), so alternating gave 2.5 fps per side with three cores idle. On the dev machine parallel cut a two-side beat from 34.8 ms to 23.9 ms (≈ the slower side). On the device, with `parallel`, `decode_on_demand` and `inference_threads = 2` all on: 4.9–5.0 fps per side while nobody is gesturing, about 3.8–4.2 fps during a gesture (≈205 ms inference plus ≈50 ms waiting for the camera's next frame). Re-read `/api/state` after any change here rather than assuming. The phase sweep at that rate: a 0.6 s gesture fires 50% instead of 100%, a 0.5 s one 25%. At ≥4 fps per side the existing `hold_*` values still fire 100% for ≥0.5 s gestures; below that they must be re-derived. Both switches default to off so the dev pipeline is unchanged — do not remove them from `config.pi.toml`. Spec §6 v1.3.
- **Recognition is slowest exactly when someone is gesturing, and `inference_threads = 2` is the Pi 4's ceiling.** Measured on the device (0.10.18, 53 °C, no throttling), one recognizer: no hand 122 ms, one hand 297 ms, two hands 214 ms — one hand is the worst case because `num_hands = 2` re-runs palm detection every frame to look for the second. With 2 XNNPACK threads: 77 / 185 / 136 ms, identical gestures and scores. With both sides busy, 2 threads each gives a 269 ms beat, 3 each 397 ms, 4 each 1580 ms — four cores, two recognizers. MediaPipe's Python `BaseOptions` does not expose the thread count; `recognition.threaded_base_options` patches it into the 0.10.x proto and falls back to the default on 1.x (no `to_pb2`). Heat is not the limiter: a fan took the SoC from 77 °C to 54 °C and the frame rate did not move.
- **In parallel mode only `_acquire` runs off the main thread, and it must stay read-only.** `GestureFSM`, `ScoreBoard` writes, fps/timing state and preview all happen in `_commit` on the main thread after every side's job has returned. Each side has its own single-thread executor so a recognizer instance is only ever called from one thread; a shared recognizer (`per_source_recognizer = false`) silently falls back to alternating.
- **On-demand decode keeps grabbing but only decodes on request.** `CameraSource._capture_on_demand` calls `grab()` continuously (dequeue, no decode) so the driver queue never holds stale frames, and `retrieve()`s only when `read()` asks. Two 1080p MJPEG streams at 30 fps cost more than a core to decode on the Pi 4 while only 5 of every 30 frames were used. `read()` then blocks for up to a frame interval, which is why the 60 Hz dev camera profile leaves it off. DShow's `grab()` never blocks, so the idle loop must sleep; a stalled camera must not hold up `read()` twice (`_waiting`).

- **Video files must track the wall clock, not decode sequentially.** The loop polls a side at ~5 fps while footage is ~30 fps; reading the next frame each tick would slow playback 6× and turn "hold 1 second" into 6 seconds of video. `VideoFileSource._read_realtime` computes the target frame from elapsed time and skips (or seeks) to it. Any change here silently corrupts scoring timing — `tests/test_video_source.py` guards it.
- **MediaPipe VIDEO mode needs strictly increasing timestamps per recognizer instance.** `GestureReader._next_timestamp` enforces this, including two frames landing in the same millisecond.
- **`per_source_recognizer` must stay `true` on both configs.** Spec §6 originally called for one shared instance to save memory; measurement killed that. Alternating two different scenes through one VIDEO-mode recognizer pollutes its hand-tracking state — `Thumb_Down` detection dropped 77% and only 88% of frames agreed with the per-source result. Two instances cost ~70 MB (283 → 352 MB), which a 2 GB Pi absorbs. Spec §6 carries the correction; do not "optimize" this back.
- **Recognition cost is fixed, not resolution-driven.** 0.92 MP vs 0.11 MP input both measure ~22.6 ms — MediaPipe rescales to 192×192 internally for palm detection. Downscaling frames buys nothing, and it is why cropping the ROI costs no extra CPU. Inference is ~78% of loop wall time, so shaving Python-side work has limited headroom.
- **`num_hands = 2` is load-bearing.** Dropping to 1 cuts inference wall time 41% but loses borderline detections — `Pointing_Up` fell from 10 to 7 hits on real footage. Since `GestureFSM` requires an *unbroken* hold, a single dropped frame restarts the timer, so lost frames cost far more than their raw percentage.
- **The frame is cropped, not downscaled** (`roi_rect`/`crop_roi`). Capturing 1080p and keeping a sub-region preserves pixel density on a distant hand — that is what makes the detection distance work. Resizing instead defeats the design.
- **What decides detection is the hand's fraction of the ROI's *long side*, not its pixel count** — MediaPipe letterboxes the ROI into 192×192, so the long side sets the scale. Measured cliff: 33% → 97% detected, 14% → 86%, 8% → 73%, 4% → 0%. Shrinking only the ROI's short side changes nothing.
- **The ROI is positionable, and its position matters more than its size** (`crop_cx_ratio`/`crop_cy_ratio`, default 0.5/0.5 = centred). On `videos/A.mp4` (real court, 1920×1080) the 13 hand-verified deliberate gestures all land in x 541–1511, y 289–438 — centroid (870, 363), i.e. 177 px *above* frame centre, because players raise a hand beside their head while the lower half of the frame is bare floor. Same long side of 768 px: centred fires 3/13, offset onto the gesture zone fires 9/13. Full frame fires 0/13. Re-derive the centre from footage before touching the ratios.
- **ROI is per-source, not global** (`SourceConfig.crop_for`, falling back field-by-field to `[capture]`). The two dev clips prove why: A is landscape real-court, B is a handheld portrait selfie. Forcing A's ROI onto B took B from 5 scoring events to 0 detections in 482 samples.
- **MJPEG is mandatory on real cameras.** `CameraSource` sets the resolution first and then FOURCC (DShow ignores MJPG until the resolution is fixed); without MJPG the camera falls back to raw YUYV and 1080p starves the USB bus. `CAP_PROP_BUFFERSIZE=1` is what makes `read()` return the newest frame instead of a queued stale one.
- **Camera `device` should be a stable path on the Pi, not a number.** `/dev/videoN` follows USB enumeration order; a swap does not crash, it silently scores points for the wrong half-court. `device` accepts `/dev/v4l/by-id/...<serial>-video-index0` (the Waveshare OV2735 enumerates as `openaicam` with a unique serial) and `CameraSource.resolve_device` turns the symlink into the V4L2 index.
- **One dead source must not kill the other half-court** (spec §5.4). `build_sources` drops what it cannot open, `_tick` skips failed reads, and `CompositeDisplay` swallows per-backend exceptions. Preserve that.
- **Anti-false-trigger logic lives entirely in `GestureFSM`** — hold-to-confirm, per-side cooldown, and the confidence gate in `GestureReader`. It is pure logic with injected timestamps; test it there rather than through the engine.
- **`hold_grace_seconds` and `hold_min_samples` are a pair; never ship one without the other.** At 5 fps per side a 0.6 s gesture yields only 3 samples, and the FSM measures the span between first and last *sample*, which is up to one sample period short of the true duration — that is why grace exists. But grace alone lets a single-frame misdetection keep the timer running until it reaches `hold_seconds`: phase-sweep simulation measured a 98% false-trigger rate for 0.05–0.2 s flickers. `hold_min_samples` requires real detections (grace samples don't count) and drops that to 0% while genuine ≥0.4 s gestures stay at 100%.
- **Cross-thread mutation goes through a queue.** Manual +1/−1 from the web UI is enqueued and applied in the main loop (`_drain_manual`), never applied directly from the Flask thread.
- **`GestureFSM` runs on `time.monotonic()`, never `time.time()`.** The Pi has no RTC; it boots from `fake-hwclock` at a stale time and steps whenever `systemd-timesyncd` reaches a network. A forward step would void every in-flight cooldown at once, a backward step would freeze a half-court. Wall-clock time belongs only in `Snapshot.updated_at`, which is a display value.
- **Replayed frames must be fed with `fresh=False`.** `_tick_side` reuses the cached `Observation` when a source hands back the same `Frame`; counting those replays as real detections lets one misdetected frame satisfy `hold_min_samples` and score. Timing still advances on a replay — only the detection count is withheld.
- **`_tick` and `_telemetry` each guard per-side, `close()` guards per-step.** Spec §5.4 isolation must hold against *exceptions*, not just against `read()` returning `None` — the fragile third-party code (MediaPipe timestamp faults, `cv2.error` on device removal) lives in the recognition path, not the capture path.
- **The web port probe needs `SO_REUSEADDR` on Linux and must not have it on Windows.** `web._port_conflict` runs before Flask starts so a taken port is reported instead of werkzeug dying silently in its thread. Without the option on Linux, a service restart found the previous process's SSE/preview connections in TIME_WAIT, reported port 8080 as taken and left the dashboard down while scoring kept running — which is how it failed on the device. On Windows the option lets the bind succeed on a port someone is listening on.
- **Display work is gated on there actually being a consumer.** `WebDisplay.wants_preview` is a property that is only true while an MJPEG stream is open, and `update()` skips `to_dict()`/`json.dumps` when no SSE client is subscribed. `CompositeDisplay.wants_preview` aggregates children, so it must stay a property — do not cache it into an attribute.
- **The LED's bottom row is a status bar, and on a single-colour matrix pattern and direction carry the meaning.** CP437 digits only use rows 0–6. Solid line growing from a side's outer edge = the FSM is accumulating that side's gesture (lit from the first detection — without the web page this is the only feedback before the score changes); dotted line shrinking back to that edge = that side's cooldown; static solid half-width line = match point, which yields the whole row while any bar is showing. One active side uses all 32 px, two share 16 px each. `LedDisplay._bar_pixels` extrapolates from the last telemetry so the render thread can animate at 20 Hz while telemetry arrives at 4–5 Hz, redraws only when a pixel count changes, and stops extrapolating after 1 s so a stalled main loop cannot drain a cooldown bar on its own. `docs/screenshots/led-status-bar.png` is rendered by the real `_draw` through luma's dummy device. The MAX7219 chain is only stable at `bus_speed_hz = 500000` on the device (3.3 V logic into a 5 V part, no level shifter); at 8 MHz blocks drop out at random.
- **Only MediaPipe's built-in labels are used** (`Victory`, `Thumb_Down`, `Open_Palm`, see `fsm.GESTURE_ACTIONS`) — there is no training step, so a new command means picking another pre-trained label.

## Working on Windows

`CameraSource` picks DShow on Windows and V4L2 on Linux, so camera code is importable here, but the LED backend (`luma`, SPI) is Pi-only and is imported lazily inside `LedDisplay.start()`. Keep hardware imports lazy so the dev machine can import every module. Verify changes with `pytest` plus `acepoint --display console --duration N`, which needs no hardware at all.
