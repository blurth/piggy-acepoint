"""Validate the offline audio pack without a sound card."""
from pathlib import Path
import argparse
from acepoint.audio.player import Pack


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=str(Path(__file__).resolve().parents[1] / "audio"))
    args = parser.parse_args()
    pack = Pack(args.path)
    print(f"OK: {len(pack.clips)} clips, {sum(c['path'].stat().st_size for c in pack.clips) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
