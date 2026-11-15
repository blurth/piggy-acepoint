#!/bin/bash
# 树莓派 4 专用修复：mediapipe 1.x 的 aarch64 安装包用了 AES 指令，而 Pi 4 的
# BCM2711 没有 ARMv8 加密扩展，一启动就被 SIGILL 杀掉：
#   FATAL ERROR: This binary was compiled with aes enabled, but this feature is
#   not available on this processor
# 0.10.x 的 aarch64 包最高只到 Python 3.12，而系统 Python 是 3.13。
#
# 本脚本：用 uv 装一个独立的 Python 3.12（不动系统 Python），逐个试 0.10.x，
# 挑第一个能在本机真正加载手势模型的版本，重建 ~/scorer，重启 acepoint 服务。
# 可重复运行。
#
# 用法（普通用户运行，中途会要 sudo 密码）：
#   bash deploy/pi4_mediapipe_fix.sh

set -u
INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
APP="$HOME/AcePoint"
VENV="$HOME/scorer"
OLD="$HOME/scorer-py313"            # 原来的 3.13 虚拟环境，留着当 uv 的来源和备份
MODEL="$APP/models/gesture_recognizer.task"
CANDIDATES="0.10.18 0.10.15 0.10.14"
LOG="$HOME/pi4fix.log"

exec > >(tee -a "$LOG") 2>&1
step() { echo; echo "=== $*"; }
fail() { echo; echo "!!! 失败：$*"; echo "完整日志：$LOG"; exit 1; }

step "检查"
[ -f "$MODEL" ] || fail "找不到 $MODEL —— 先完成 SD 卡安装"
uname -m

step "停止 acepoint 服务"
sudo systemctl stop acepoint

step "准备 uv"
if [ ! -d "$OLD" ]; then
    [ -x "$VENV/bin/pip" ] || fail "找不到 $VENV/bin/pip"
    "$VENV/bin/pip" install -q -i "$INDEX" uv || fail "安装 uv 失败"
    mv "$VENV" "$OLD" || fail "备份旧虚拟环境失败"
fi
UV="$OLD/bin/uv"
[ -x "$UV" ] || fail "uv 不在 $UV"
"$UV" --version
rm -rf "$VENV"

step "安装 Python 3.12（先走国内镜像）"
UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone \
    "$UV" python install 3.12 \
    || { echo "镜像失败，改用 GitHub 直连"; "$UV" python install 3.12; } \
    || fail "安装 Python 3.12 失败"

step "建虚拟环境 $VENV"
"$UV" venv --python 3.12 "$VENV" || fail "建虚拟环境失败"
PY="$VENV/bin/python"
"$PY" --version

step "挑选能在这台 Pi 4 上运行的 mediapipe"
GOOD=""
for v in $CANDIDATES; do
    echo "--- 试 mediapipe==$v"
    if ! "$UV" pip install --python "$PY" -i "$INDEX" -q "mediapipe==$v"; then
        echo "装不上，跳过"
        continue
    fi
    "$PY" - "$MODEL" <<'EOF'
import sys
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision

recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
    base_options=mpp.BaseOptions(model_asset_path=sys.argv[1]),
    running_mode=vision.RunningMode.VIDEO, num_hands=2))
image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((480, 640, 3), np.uint8))
recognizer.recognize_for_video(image, 0)
print("模型加载并推理成功，mediapipe", mp.__version__)
EOF
    rc=$?
    if [ $rc -eq 0 ]; then
        GOOD="$v"
        break
    fi
    echo "mediapipe $v 在本机不能运行（退出码 $rc；132 = 非法指令）"
done
[ -n "$GOOD" ] || fail "候选版本都不能在这台 Pi 4 上运行"
echo "选定 mediapipe==$GOOD"

step "安装项目及依赖（锁定 mediapipe==$GOOD）"
CONSTRAINTS=$(mktemp)
echo "mediapipe==$GOOD" > "$CONSTRAINTS"
"$UV" pip install --python "$PY" -i "$INDEX" -c "$CONSTRAINTS" -e "$APP[pi]" \
    || fail "安装项目依赖失败"
"$PY" -c "import cv2, mediapipe, numpy; print('cv2', cv2.__version__, '| mediapipe', mediapipe.__version__, '| numpy', numpy.__version__)" \
    || fail "依赖导入失败"

step "环境自检"
cd "$APP" && "$VENV/bin/acepoint" -c config.pi.toml --check
echo "自检退出码 $?"

step "启动 acepoint 服务"
sudo systemctl start acepoint
sleep 30
systemctl status acepoint --no-pager -n 20

step "完成。网页计分板：http://$(hostname -I | awk '{print $1}'):8080"
echo "完整日志：$LOG"
