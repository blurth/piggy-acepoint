# -*- coding: utf-8 -*-
"""
羽毛球手势计分系统 · 树莓派4版
================================
双USB摄像头轮询 + 识别区裁剪 + MediaPipe手势识别 + MAX7219点阵显示

安装(在Pi 4上):
  sudo raspi-config   # Interface Options -> SPI -> Enable
  python3 -m venv ~/scorer && source ~/scorer/bin/activate
  pip install mediapipe opencv-python luma.led_matrix
  wget -O gesture_recognizer.task https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task

运行:
  python badminton_pi4.py
  加 --no-led 可在没接点阵屏时调试(比分打印到终端)
  加 --preview 可在有桌面环境时显示摄像头画面(调试识别区用)

开机自启(systemd):
  sudo tee /etc/systemd/system/scorer.service <<'EOF'
  [Unit]
  Description=Badminton Scorer
  After=multi-user.target
  [Service]
  User=pi
  WorkingDirectory=/home/pi
  ExecStart=/home/pi/scorer/bin/python /home/pi/badminton_pi4.py
  Restart=always
  [Install]
  WantedBy=multi-user.target
  EOF
  sudo systemctl enable --now scorer
"""

import argparse
import sys
import threading
import time

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

# ================= 配置区 =================
MODEL_PATH = "gesture_recognizer.task"

CAMERAS = {                # 半场 -> V4L2设备号。ls /dev/video* 查看;一般两个摄像头是 0 和 2
    "left": 0,
    "right": 2,
}
CAPTURE_W, CAPTURE_H = 1920, 1080   # 采集分辨率(需摄像头支持MJPEG)
CROP_W_RATIO = 0.6                  # 识别区:画面中央宽60%
CROP_H_RATIO = 0.8                  # 高80%(去掉天花板和地面)

POLL_HZ = 10               # 总轮询频率(双摄各分到约5fps)
HOLD_SECONDS = 1.0         # 加分/撤销手势保持时长
RESET_HOLD = 3.0           # 清零手势保持时长
COOLDOWN_SECONDS = 8.0     # 计分后冷却
MIN_SCORE = 0.6            # 手势置信度阈值

LED_BRIGHTNESS = 60        # 0~255
# ==========================================

score = {"left": 0, "right": 0}
score_lock = threading.Lock()
flash_event = threading.Event()   # 通知LED闪烁


class GestureFSM:
    """单个半场的手势状态机:持续确认 + 冷却"""

    def __init__(self, side: str):
        self.side = side
        self.current = None
        self.since = 0.0
        self.cooldown_until = 0.0

    def feed(self, gesture, now):
        if now < self.cooldown_until:
            self.current = None
            return
        if gesture != self.current:
            self.current = gesture
            self.since = now
            return
        if gesture is None:
            return
        held = now - self.since
        if gesture == "Victory" and held >= HOLD_SECONDS:
            self._apply(+1, now)
        elif gesture == "Thumb_Down" and held >= HOLD_SECONDS:
            self._apply(-1, now)
        elif gesture == "Open_Palm" and held >= RESET_HOLD:
            self._reset(now)

    def _apply(self, delta, now):
        with score_lock:
            score[self.side] = max(0, score[self.side] + delta)
            snap = dict(score)
        self.cooldown_until = now + COOLDOWN_SECONDS
        self.current = None
        print(f"[{time.strftime('%H:%M:%S')}] {self.side} {'+1' if delta>0 else '-1'} -> {snap['left']}:{snap['right']}")
        flash_event.set()

    def _reset(self, now):
        with score_lock:
            score["left"] = score["right"] = 0
        self.cooldown_until = now + COOLDOWN_SECONDS
        self.current = None
        print(f"[{time.strftime('%H:%M:%S')}] RESET by {self.side}")
        flash_event.set()


def open_camera(dev: int):
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_H)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # 丢弃积压帧,永远取最新画面
    if not cap.isOpened():
        print(f"警告: /dev/video{dev} 打开失败,该半场停用")
        return None
    return cap


def crop_roi(frame):
    """裁剪中央识别区,保留原始像素密度"""
    h, w = frame.shape[:2]
    cw, ch = int(w * CROP_W_RATIO), int(h * CROP_H_RATIO)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return frame[y0:y0 + ch, x0:x0 + cw]


def led_worker(no_led: bool):
    """点阵屏渲染线程。比分变化即刷新,计分时闪烁2次"""
    last = None
    if no_led:
        while True:
            with score_lock:
                snap = (score["left"], score["right"])
            if snap != last:
                print(f"  [LED] {snap[0]}:{snap[1]}")
                last = snap
            time.sleep(0.2)

    from luma.core.interface.serial import spi, noop
    from luma.led_matrix.device import max7219
    from luma.core.render import canvas
    from luma.core.legacy import text
    from luma.core.legacy.font import proportional, LCD_FONT

    serial = spi(port=0, device=0, gpio=noop())
    device = max7219(serial, cascaded=4, block_orientation=-90)
    device.contrast(LED_BRIGHTNESS)

    def draw(snap, blank=False):
        with canvas(device) as c:
            if not blank:
                text(c, (1, 1), f"{snap[0]}:{snap[1]}", fill="white",
                     font=proportional(LCD_FONT))

    while True:
        with score_lock:
            snap = (score["left"], score["right"])
        if flash_event.is_set():
            flash_event.clear()
            for _ in range(2):
                draw(snap, blank=True); time.sleep(0.15)
                draw(snap); time.sleep(0.15)
            last = snap
        elif snap != last:
            draw(snap)
            last = snap
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-led", action="store_true", help="不使用点阵屏,比分打印到终端")
    ap.add_argument("--preview", action="store_true", help="显示识别区画面(需桌面环境)")
    args = ap.parse_args()

    base = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    recognizer = vision.GestureRecognizer.create_from_options(
        vision.GestureRecognizerOptions(
            base_options=base,
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
        ))

    cams, fsms = {}, {}
    for side, dev in CAMERAS.items():
        cap = open_camera(dev)
        if cap:
            cams[side] = cap
            fsms[side] = GestureFSM(side)
    if not cams:
        sys.exit("没有可用摄像头,退出")

    threading.Thread(target=led_worker, args=(args.no_led,), daemon=True).start()

    interval = 1.0 / POLL_HZ
    ts_ms = 0
    print(f"就绪: {list(cams)} | 轮询{POLL_HZ}Hz | 识别区{int(CROP_W_RATIO*100)}%x{int(CROP_H_RATIO*100)}%")

    while True:
        for side, cap in cams.items():
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                continue
            roi = crop_roi(frame)
            rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts_ms += 1   # VIDEO模式要求时间戳单调递增即可
            result = recognizer.recognize_for_video(mp_img, ts_ms)

            gesture = None
            if result.gestures:
                top = result.gestures[0][0]
                if top.score >= MIN_SCORE and top.category_name != "None":
                    gesture = top.category_name
            fsms[side].feed(gesture, time.time())

            if args.preview:
                disp = cv2.resize(roi, (640, int(640 * roi.shape[0] / roi.shape[1])))
                if gesture:
                    cv2.putText(disp, gesture, (10, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                cv2.imshow(side, disp)
                cv2.waitKey(1)

            spent = time.time() - t0
            if spent < interval:
                time.sleep(interval - spent)


if __name__ == "__main__":
    main()
