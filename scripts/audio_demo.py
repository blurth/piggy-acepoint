"""Interactive scoreboard/audio demo without cameras or MediaPipe.

python scripts/audio_demo.py --port 8089
"""
import argparse
from dataclasses import replace
import logging
from pathlib import Path
import queue
import time

from acepoint.audio.service import AudioService
from acepoint.config import load_config, WebConfig
from acepoint.displays.web import WebDisplay
from acepoint.state import ScoreBoard, Telemetry


def main():
    parser = argparse.ArgumentParser(description="Camera-free audio demo")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("-c", "--config", default=str(Path(__file__).resolve().parents[1] / "config.toml"))
    parser.add_argument("--silent", action="store_true", help="Use the null player for UI tests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    audio_config = replace(config.audio, enabled=True, allow_web_control=True,
                           backend="null" if args.silent else config.audio.backend)
    board = ScoreBoard(config.game)
    audio = AudioService(audio_config, config.game, config.resolve(audio_config.pack_root))
    commands = queue.Queue(maxsize=32)

    def enqueue(command):
        try:
            commands.put_nowait(command)
            return True
        except queue.Full:
            return False

    display = WebDisplay(WebConfig(port=args.port, preview=False), audio_config=audio_config,
                         on_audio=lambda body: enqueue(("audio", body)),
                         on_action=lambda side, action: enqueue(("score", (side, action))))
    display.start()
    try:
        while True:
            try:
                kind, body = commands.get(timeout=.05)
                if kind == "score":
                    side, action = body
                    before, after = board.apply_with_change(side, action, source="manual")
                    audio.score(side, action, before, after)
                elif body == {"test": True}:
                    audio.preview(board.snapshot())
                else:
                    audio.configure(body)
            except queue.Empty:
                pass
            display.update(Telemetry(board.snapshot(), audio=audio.snapshot()))
    except KeyboardInterrupt:
        pass
    finally:
        audio.close()
        display.close()


if __name__ == "__main__":
    main()
