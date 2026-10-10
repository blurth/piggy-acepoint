import cv2
import time


def capture(dev, width=1920, height=1080, n=10):
    cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        print(f"camera #{dev}: failed to open")
        return None
    # warm up / drain buffer
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        time.sleep(0.05)
    cap.release()
    return frame if ok else None


for dev, label in [(0, "left"), (1, "right")]:
    frame = capture(dev)
    if frame is not None:
        path = f"camera_{label}.jpg"
        cv2.imwrite(path, frame)
        h, w = frame.shape[:2]
        print(f"saved {path}: {w}x{h}")
    else:
        print(f"camera {label}: no frame")
