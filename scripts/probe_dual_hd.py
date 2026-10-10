import time
import cv2


def open_cam(dev, width, height):
    cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    ok, _ = cap.read()
    if not ok:
        cap.release()
        return None
    return cap


def bench(cap, n=30):
    t0 = time.monotonic()
    ok = 0
    for _ in range(n):
        r, _ = cap.read()
        if r:
            ok += 1
    return ok, time.monotonic() - t0


for res in [(1920, 1080), (1280, 720)]:
    print(f"\n=== {res[0]}x{res[1]} MJPG dual ===")
    caps = []
    for dev, label in [(0, "left"), (1, "right")]:
        cap = open_cam(dev, *res)
        if cap is None:
            print(f"  {label}(#{dev}): FAILED")
            for c in caps:
                c.release()
            break
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"  {label}(#{dev}): {w}x{h}")
        caps.append((label, cap))
    else:
        for label, cap in caps:
            ok, elapsed = bench(cap)
            print(f"  {label}: {ok}/30 frames in {elapsed:.2f}s = {ok/elapsed:.1f}fps")
    for _, cap in caps:
        cap.release()
