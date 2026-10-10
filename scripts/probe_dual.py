import time
import cv2


def open_cam(dev, width, height, order="res_first"):
    cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return None, None
    if order == "res_first":
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    else:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    ok, frame = cap.read()
    if not ok:
        cap.release()
        return None, None
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    fmt = "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4)).strip()
    return cap, (w, h, fmt)


def bench(cap, n=30):
    t0 = time.monotonic()
    ok = 0
    for _ in range(n):
        r, _ = cap.read()
        if r:
            ok += 1
    elapsed = time.monotonic() - t0
    return ok, elapsed


def test(name, order, res, reverse=False):
    w, h = res
    devices = [(0, "left"), (1, "right")]
    if reverse:
        devices = list(reversed(devices))
    print(f"\n{name} {order} {res} order={['0->1','1->0'][reverse]}")
    caps = {}
    for dev, label in devices:
        cap, info = open_cam(dev, w, h, order)
        if cap is None:
            print(f"  {label}(#{dev}): FAILED to open")
            for c in caps.values():
                c.release()
            return
        print(f"  {label}(#{dev}): {info[0]}x{info[1]} [{info[2]}]")
        caps[label] = cap
    # bench
    for label, cap in caps.items():
        ok, elapsed = bench(cap)
        print(f"  {label}: {ok}/30 frames in {elapsed:.2f}s = {ok/elapsed:.1f}fps")
    for c in caps.values():
        c.release()


for res in [(640, 480), (320, 240)]:
    for order in ["res_first", "fourcc_first"]:
        for reverse in [False, True]:
            test("", order, res, reverse)

# also test yuy2 both open
print("\n=== YUY2 640x480 both open ===")
for dev, label in [(0, "left"), (1, "right")]:
    cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    ok, _ = cap.read()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    fmt = "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4)).strip()
    print(f"  {label}(#{dev}): {w}x{h} [{fmt}]")
    ok, elapsed = bench(cap)
    print(f"  {label}: {ok}/30 frames in {elapsed:.2f}s = {ok/elapsed:.1f}fps")
    cap.release()
