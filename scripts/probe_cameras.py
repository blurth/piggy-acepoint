import cv2

def test(dev, backend):
    cap = cv2.VideoCapture(dev, backend)
    if not cap.isOpened():
        return None
    # try setting MJPG + 1920x1080
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    fmt = "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4)).strip()
    cap.release()
    return w, h, fmt

def test_res(dev, backend, w_in, h_in):
    cap = cv2.VideoCapture(dev, backend)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w_in)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h_in)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    fmt = "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4)).strip()
    cap.release()
    return w, h, fmt

print("Probing cameras (DirectShow) ...")
for i in range(2):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"camera {i}: not available")
        continue
    cap.release()
    print(f"\ncamera {i}:")
    for res in [(1920, 1080), (1280, 720), (640, 480), (320, 240)]:
        w, h, fmt = test_res(i, cv2.CAP_DSHOW, *res)
        print(f"  requested {res[0]}x{res[1]} -> actual {w}x{h} [{fmt}]")
