"""Probe Windows camera indices through each OpenCV backend."""

import cv2


BACKENDS = (
    ("DSHOW", cv2.CAP_DSHOW),
    ("MSMF", cv2.CAP_MSMF),
    ("ANY", cv2.CAP_ANY),
)


for backend_name, backend in BACKENDS:
    print(f"\n=== {backend_name} ===")
    for device in range(6):
        cap = cv2.VideoCapture(device, backend)
        opened = cap.isOpened()
        ok, frame = cap.read() if opened else (False, None)
        if opened:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"camera {device}: open={opened} read={ok} {width}x{height}")
        cap.release()
