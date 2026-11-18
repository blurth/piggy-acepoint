#!/bin/bash
# 树莓派上更新 AcePoint：拉代码 -> 拷进 ~/AcePoint -> 补配置项 -> 自检 -> 重启服务。
#
# ~/AcePoint 是从 SD 卡解出来的，不是 git 仓库，所以代码走这份 git 克隆中转。
# 现场改过的 config.pi.toml 不会被覆盖，只补上缺的配置项（已有的不动）。
#
# 用法（普通用户运行，中途会要 sudo 密码）：
#   cd ~/piggy-acepoint && git pull && bash deploy/pi_update.sh

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/AcePoint"
VENV="$HOME/scorer"
CONF="$APP/config.pi.toml"
LOG="$HOME/pi-update.log"

exec > >(tee "$LOG") 2>&1
step() { echo; echo "=== $*"; }
fail() { echo; echo "!!! 失败：$*"; echo "完整日志：$LOG"; exit 1; }

if grep -q "overlayroot=tmpfs" /proc/cmdline; then
    echo
    echo "!! 只读文件系统已开启：这次更新只写进内存，重启后就没了（可以当试运行）。"
    echo "   要让更新留在 SD 卡上，按 deploy/README.md 第 7 节走一遍维护流程："
    echo "   关只读 -> 重启 -> 更新 -> 开只读 -> 重启"
fi

[ -d "$APP/src" ] || fail "找不到 $APP/src —— 先完成安装"
[ -x "$VENV/bin/acepoint" ] || fail "找不到 $VENV/bin/acepoint —— 先完成安装"

step "拉取最新代码"
git -C "$REPO" pull --ff-only || echo "git pull 没成功，继续用本地这份代码"
git -C "$REPO" log --oneline -1

step "拷贝代码（不动配置文件）"
for dir in src scripts deploy; do
    mkdir -p "$APP/$dir"
    cp -r "$REPO/$dir/." "$APP/$dir/" || fail "拷贝 $dir 失败"
done

step "补配置项"
# ensure <锚点行的正则> <配置项> <值>：配置项不存在时，加在锚点行的下一行
ensure() {
    if grep -qE "^$2[[:space:]]*=" "$CONF"; then
        echo "已有  $(grep -E "^$2[[:space:]]*=" "$CONF" | head -1)"
    elif grep -qE "$1" "$CONF"; then
        sed -i "/$1/a $2 = $3" "$CONF" && echo "新增  $2 = $3"
    else
        echo "!! 没找到锚点 $1，$2 没加上 —— 请手动加到 $CONF"
    fi
}
ensure '^per_source_recognizer' parallel true
ensure '^parallel[[:space:]]*=' inference_threads 2
ensure '^poll_hz' decode_on_demand true
# 整段新增的配置表：不存在就把仓库里那一段原样追加到末尾
if grep -q '^\[record\]' "$CONF"; then
    echo "已有  [record]"
else
    { echo; sed -n '/^\[record\]/,/^\[/p' "$REPO/config.pi.toml" | sed '$d'; } >> "$CONF" \
        && echo "新增  [record]（边计分边录像，见 config.pi.toml 注释）"
fi

step "停止服务并自检"
sudo systemctl stop acepoint
(cd "$APP" && "$VENV/bin/acepoint" -c config.pi.toml --check)
echo "自检退出码 $?"

step "启动服务"
sudo systemctl start acepoint
sleep 25
systemctl is-active acepoint
journalctl -u acepoint -n 60 --no-pager \
    | grep -E "acepoint\.(sources|engine|displays\.led|recognition)|Traceback|Error" | tail -12

step "完成。网页计分板：http://$(hostname -I | awk '{print $1}'):8080"
echo "完整日志：$LOG"
