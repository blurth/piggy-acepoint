#!/bin/sh
# 一次性安装器：由 cmdline.txt 末尾的 systemd.run= 在开机时调用（和 Raspberry Pi
# Imager 的 firstrun.sh 同一机制）。装好 acepoint-diag.service，把 cmdline.txt
# 还原，然后由 systemd.run_success_action=reboot 重启进入正常系统。

BOOT=/boot/firmware
[ -f "$BOOT/cmdline.txt" ] || BOOT=/boot
DIAG="$BOOT/acepoint-diag"
mkdir -p "$DIAG/logs"
LOG="$DIAG/logs/install.log"

# 第一件事就还原 cmdline：后面无论成败，都不会反复进安装模式
sed -i 's| systemd\.run[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' "$BOOT/cmdline.txt"

{
    echo "=== install $(date) ==="
    echo "cmdline 已还原为: $(cat "$BOOT/cmdline.txt")"
    sed "s|/boot/firmware/|$BOOT/|g" "$DIAG/acepoint-diag.service" \
        > /etc/systemd/system/acepoint-diag.service
    chmod 644 /etc/systemd/system/acepoint-diag.service
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /etc/systemd/system/acepoint-diag.service \
        /etc/systemd/system/multi-user.target.wants/acepoint-diag.service
    ls -l /etc/systemd/system/acepoint-diag.service \
        /etc/systemd/system/multi-user.target.wants/acepoint-diag.service
    echo "安装完成，即将重启"
} >> "$LOG" 2>&1

sync
exit 0
