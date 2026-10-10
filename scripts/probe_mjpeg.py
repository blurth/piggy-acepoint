import cv2

BACKENDS = [
    ("DSHOW", cv2.CAP_DSHOW),
    ("MSMF", cv2.CAP_MSMF),
    ("ANY", cv2.CAP_ANY),
]

FOURCCS = ["MJPG", "MJPEG", "MPEG", "XVID", "mp4v", "H264", "YUY2"]


def try_config(dev, backend_name, backend, fourcc, w_in, h_in, order="fourcc_first"):
    cap = cv2.VideoCapture(dev, backend)
    if not cap.isOpened():
        cap.release()
        return None

    fcc = cv2.VideoWriter_fourcc(*fourcc)
    if order == "fourcc_first":
        cap.set(cv2.CAP_PROP_FOURCC, fcc)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w_in)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h_in)
    elif order == "res_first":
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w_in)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h_in)
        cap.set(cv2.CAP_PROP_FOURCC, fcc)
    elif order == "reopen":
        cap.release()
        cap = cv2.VideoCapture(dev, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w_in)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h_in)
        cap.set(cv2.CAP_PROP_FOURCC, fcc)

    # read a frame to let the driver settle
    ok, _ = cap.read()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    fmt = "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4)).strip()
    cap.release()
    return ok, w, h, fmt


def main():
    for dev in range(2):
        cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print(f"camera {dev}: not available\n")
            continue
        cap.release()
        print(f"\n=== camera {dev} ===")
        for backend_name, backend in BACKENDS:
            for fourcc in FOURCCS:
                for res in [(1920, 1080), (1280, 720), (640, 480)]:
                    for order in ["fourcc_first", "res_first"]:
                        result = try_config(dev, backend_name, backend, fourcc, *res, order)
                        if result is None:
                            continue
                        ok, w, h, fmt = result
                        if ok and fmt == fourcc:
                            print(f"OK  backend={backend_name:5} fourcc={fourcc:6} order={order:12} requested={res[0]}x{res[1]} actual={w}x{h} [{fmt}]")


if __name__ == "__main__":
    main()
