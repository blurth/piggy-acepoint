#!/bin/sh
# 从 SD 卡启动分区安装 AcePoint：不需要 SSH，不需要 git。
# 由 acepoint-diag 的开机诊断在每次开机时检查并启动（以 root 运行），
# 成功一次后写 /var/lib/acepoint/setup.done，以后不再执行。
# 失败（比如没联网）不写标记，下次开机自动重试。
# 日志写回启动分区：acepoint-setup/logs/，拔卡到 Windows 上看。

set -u
BOOT=/boot/firmware
[ -d "$BOOT/acepoint-setup" ] || BOOT=/boot
SETUP="$BOOT/acepoint-setup"
mkdir -p "$SETUP/logs"
N=$(ls "$SETUP/logs" | grep -c '^setup-')
LOG="$SETUP/logs/setup-$(printf %02d $((N + 1))).txt"
exec >>"$LOG" 2>&1

step() { echo; echo "=== [开机 $(cut -d' ' -f1 /proc/uptime) 秒] $*"; sync; }
fail() { echo; echo "!!! 安装失败：$*"; echo "（下次开机会自动重试）"; sync; exit 1; }

step "开始安装 AcePoint"
USER_NAME=$(id -nu 1000 2>/dev/null) || fail "找不到 uid 1000 的普通用户"
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
APP="$HOME_DIR/AcePoint"
VENV="$HOME_DIR/scorer"
as_user() { runuser -u "$USER_NAME" -- "$@"; }
echo "用户 $USER_NAME，项目 $APP，虚拟环境 $VENV"
python3 --version
uname -m

step "等待联网（最多 5 分钟），选 pip 源"
INDEX=""
i=0
while [ -z "$INDEX" ]; do
    for url in https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple https://pypi.org/simple; do
        if python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1] + '/pip/', timeout=10)" "$url" 2>/dev/null; then
            INDEX="$url"
            break
        fi
    done
    [ -n "$INDEX" ] && break
    i=$((i + 1))
    [ $i -ge 30 ] && { hostname -I; fail "5 分钟内连不上任何 pip 源 —— 树莓派没联网？"; }
    sleep 10
done
echo "使用 $INDEX"
hostname -I

step "解压源码和模型"
mkdir -p "$APP" || fail "建目录失败"
tar -xf "$SETUP/acepoint-src.tar" -C "$APP" || fail "解压源码失败"
mkdir -p "$APP/models"
cp "$SETUP/gesture_recognizer.task" "$APP/models/" || fail "拷模型失败"
chown -R "$USER_NAME:" "$APP"
ls "$APP"

step "建虚拟环境"
if [ ! -x "$VENV/bin/python" ]; then
    as_user python3 -m venv "$VENV" || fail "建虚拟环境失败（缺 python3-venv？）"
fi
as_user "$VENV/bin/python" --version

step "pip 安装依赖（mediapipe / opencv 较大，可能要十几分钟）"
as_user "$VENV/bin/pip" install -i "$INDEX" --timeout 120 --progress-bar off -U pip \
    || fail "升级 pip 失败"
as_user "$VENV/bin/pip" install -i "$INDEX" --timeout 120 --progress-bar off -c "$SETUP/constraints.txt" -e "$APP[pi]" \
    || fail "pip 安装依赖失败（看上面的报错；常见原因：mediapipe 没有这个 Python 版本的安装包）"

step "环境自检（摄像头、模型、点阵屏）"
cd "$APP" && as_user "$VENV/bin/acepoint" -c config.pi.toml --check
echo "自检退出码 $?（失败不阻止安装，原因看上面）"

step "安装开机自启服务（用户 $USER_NAME，点阵屏 + 网页 + 终端）"
sed -e "s|^User=.*|User=$USER_NAME|" \
    -e "s|/home/pi/|$HOME_DIR/|g" \
    -e "s|config.pi.toml\$|config.pi.toml --display led,web,console|" \
    "$APP/deploy/acepoint.service" > /etc/systemd/system/acepoint.service || fail "写服务文件失败"
grep -E '^(User|WorkingDirectory|ExecStart)=' /etc/systemd/system/acepoint.service
systemctl daemon-reload
systemctl enable acepoint || fail "启用服务失败"
systemctl restart acepoint

step "等 60 秒看服务是否跑起来"
sleep 60
systemctl status acepoint --no-pager -n 0
journalctl -u acepoint --no-pager -n 60

mkdir -p /var/lib/acepoint
touch /var/lib/acepoint/setup.done
step "安装完成。网页计分板：http://ace.local:8080 或 http://<下面的IP>:8080"
hostname -I
sync
