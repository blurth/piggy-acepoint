"""扫描采样相位：手势落在采样点之间的位置会不会影响判定。

    python scripts/check_sampling.py
    python scripts/check_sampling.py -c config.pi.toml

主循环每路只有约 5fps（采样周期 0.2 秒），而状态机比较的是**首末采样点的跨度**，
不是手势的真实时长 —— 跨度最多比真实时长短一个采样周期。所以同样长的手势，
落在不同相位上结果可能不同，必须扫遍一整个周期才知道参数配得住配不住。

改 poll_hz / hold_seconds / hold_grace_seconds / hold_min_samples 之后跑一遍。
理想形状是：短闪现 0%、真人手势时长区间 100%，中间过渡越窄越好。
"""

from __future__ import annotations

from dataclasses import replace

from _shared import build_parser

from acepoint.config import load_config
from acepoint.fsm import GestureFSM

PHASES = 60
DURATIONS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2, 1.6)


def fires(duration: float, phase: float, scoring, period: float) -> tuple[bool, int, float]:
    fsm = GestureFSM("left", scoring)
    seen: list[float] = []
    fired = False
    now = 0.0
    horizon = phase + duration + 4 * period + scoring.hold_grace_seconds
    while now < horizon:
        present = phase <= now < phase + duration
        if present:
            seen.append(now)
        if fsm.feed("Victory" if present else None, now) is not None:
            fired = True
        now += period
    span = seen[-1] - seen[0] if len(seen) > 1 else 0.0
    return fired, len(seen), span


def sweep(scoring, period: float) -> None:
    print(f"{'手势时长':>9} {'触发率':>8} {'采样点':>10} {'首末跨度':>14}")
    for duration in DURATIONS:
        results = [fires(duration, i * period / PHASES, scoring, period)
                   for i in range(PHASES)]
        rate = sum(1 for f, _, _ in results if f) / PHASES * 100
        counts = sorted({n for _, n, _ in results})
        spans = sorted({round(s, 2) for _, _, s in results})
        bar = "#" * round(rate / 5)
        print(f"{duration:8.2f}s {rate:7.0f}% {str(counts):>10} {str(spans):>14}  {bar}")


def main() -> int:
    parser = build_parser(__doc__)
    args = parser.parse_args()
    config = load_config(args.config)
    sources = max(1, len(config.sources))
    period = 1.0 / (config.capture.poll_hz / sources)
    scoring = replace(config.scoring, cooldown_seconds=0.0)   # 冷却与相位无关

    print(f"hold={scoring.hold_seconds}s  grace={scoring.hold_grace_seconds}s  "
          f"min_samples={scoring.hold_min_samples}")
    print(f"poll_hz={config.capture.poll_hz} / {sources} 路 "
          f"-> 每路 {1/period:.1f}fps，采样周期 {period:.2f}s\n")
    sweep(scoring, period)

    print("\n采样点 = 手势期间真正识别到的次数；跨度 = 首末采样点的时间差")
    print("短闪现应为 0%（靠 hold_min_samples 挡住），真人手势区间应为 100%")

    print("\n--- 去掉 hold_min_samples 会怎样 ---")
    sweep(replace(scoring, hold_min_samples=1), period)
    print("闪现那几行不再是 0% —— 容忍窗口会让计时一直跑到 hold_seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
