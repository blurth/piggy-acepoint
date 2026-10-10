# 部署到树莓派 4

对应需求文档 §10 阶段 3。按顺序做，每步都有验证手段。

## 1. 系统准备

必须是 **64 位 Raspberry Pi OS**（Bookworm）。mediapipe 不发布 32 位 armv7l 的
wheel，32 位系统会直接卡在 `pip install`。

```bash
uname -m          # 必须是 aarch64
sudo raspi-config # Interface Options -> SPI -> Enable，然后重启
```

## 2. 装程序

Bookworm 的 PEP 668 要求用虚拟环境：

```bash
git clone <repo> /home/pi/AcePoint && cd /home/pi/AcePoint
python3 -m venv /home/pi/scorer
source /home/pi/scorer/bin/activate
pip install -e ".[pi]"
pip install spidev          # luma 是惰性导入它的，缺了只在运行时才报错
python scripts/fetch_model.py
```

## 3. 接线自检（先屏后摄像头）

```bash
acepoint --led-test -c config.pi.toml
```

会依次显示：全亮 → `88 88` → 逐块点亮 → `12 34`。照着调 `config.pi.toml`：

| 现象 | 改哪个 |
|---|---|
| 数字倒置或侧躺 | `block_orientation`（0 / 90 / -90 / 180）、`rotate` |
| 左右两半反了 | `reverse_blocks = true` |
| 花屏、乱码、间歇性错位 | `bus_speed_hz` 降到 `1000000` |

> 花屏的根因是电平余量：MAX7219 接 5V 时 V_IH = 3.5V，而 Pi 的 SPI 只有 3.3V，
> 本来就低于规格。MAX7219 工作电源范围为 4.0~5.5V，不能把 VCC 改接 3.3V。
> 保持稳定 5V 供电，为 DIN、CLK、CS 添加适合 SPI 的 3.3V 到 5V 电平转换并共地。
> 降速可以改善信号质量，但不能替代电平转换。
>
> 程序初始化时同步显示 `00 00`，随后加载手势模型。`--check` 仅确认 SPI 写入成功，
> 请目视确认实际显示；接线、供电异常可能不会产生软件错误。

## 4. 摄像头自检

```bash
ls /dev/video*
acepoint --check -c config.pi.toml
```

`--check` 会**真读 10 帧**，校验分辨率、确认走的是 MJPG、报实测帧率。
只判断"能打开"是不够的：V4L2 的 `open()` 只做 QUERYCAP/S_FMT/REQBUFS，
真正的 STREAMON 在第一次 read —— 双 USB 摄像头最经典的带宽不足（ENOSPC）
只在那时才暴露。

读不到画面时，把两个摄像头分插 USB2 和 USB3 口分散带宽。

> ⚠️ **设备号会变。** `/dev/videoN` 的编号跟 USB 枚举顺序走，重启可能对调，
> 而 BOM §2 要求同型号一对（VID:PID 相同、多半没有唯一序列号）。
> 后果不是崩溃，是**左右半场静默对调**，整场比分加反。
> 装盒前请改用按物理插座寻址的稳定路径，写进 `config.pi.toml`：
>
> ```bash
> ls -l /dev/v4l/by-path/     # 形如 platform-xhci-hcd.1-usb-0:1.1:1.0-video-index0
> ```
>
> （当前 `SourceConfig.device` 只接受整数，改用路径需要先把它放宽成 `int | str`。）

## 5. 跑一次实测标定

**所有防误触参数都是在开发机上按 5fps/路 标定的**，Pi 的算力能不能维持这个
采样率必须实测。开发机单帧推理 23~26ms，Pi 4 保守估 4~8 倍慢。

```bash
python scripts/profile_pipeline.py -c config.pi.toml    # 看 recognize 那行和 RSS
python scripts/check_sampling.py -c config.pi.toml      # 按实测采样率复核相位
```

若每路实际帧率明显低于 5fps，`hold_seconds` / `hold_grace_seconds` /
`hold_min_samples` 都要按新的采样周期重新标定（需求 §10 阶段 2）。

## 6. 开机自启

```bash
sudo cp deploy/acepoint.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now acepoint
journalctl -u acepoint -f
```

unit 里 `StartLimitIntervalSec=0` 是有意的：systemd 默认 10 秒内重启 5 次就把
服务打成 failed **永不再试**，而摄像头没枚举出来时程序会立即退出，5 次轻松
挤进 10 秒。这是个无键盘无网络、只读根文件系统的球场设备，进了 failed 状态
只能拔 SD 卡救。

## 7. 只读文件系统

```bash
sudo raspi-config   # Performance Options -> Overlay File System -> Enable
```

需求 §3.3.4 要求拔电即关机不损坏 TF 卡。开启后注意两点：

- **写入不会失败，而是写进内存里的 overlay 上层，掉电即失**。所以
  `journalctl` 的日志每次断电全部丢失——"第三局中间突然不计分了"这类问题
  事后无从查起。这是这台无网设备唯一的可观测性缺口。
- 别在只读系统上加文件日志：它会在内存里无限增长，和 MediaPipe 的约 352MB
  一起挤 2GB 内存，最后 OOM。同时确认 `dphys-swapfile` 已关闭。

要留现场记录，在 SD 上单独分一个小分区挂 `noatime`，把 journald 指过去；
那个分区损坏不影响启动，正是 §3.3.4 真正要保护的东西。验收阶段也可以只在
跑测试时插一个 U 盘。

## 8. 装盒前的检查清单

- [ ] 充电宝确认是 5V/3A 档位（§3.3.1，2A 的老充电宝会在识别高负载时欠压降频）
- [ ] 供电线不超过 1 米（§3.3.3，压降会导致欠压）
- [ ] 散热片贴好；盒内无风道，持续多核负载会摸到 80°C 软降频点
- [ ] 连续拔电 10 次，每次都能重启到就绪（§9.5）
- [ ] 装盒实测续航（§9.4 要求 ≥4 小时；按实际占空比，10000mAh 偏紧，
      俱乐部活动建议 20000mAh）
