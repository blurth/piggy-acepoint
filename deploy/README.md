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

> ⚠️ **Pi 4 不能用 mediapipe 1.x。** 它的 aarch64 安装包用了 ARMv8 AES 指令，而 Pi 4 的
> BCM2711 没有加密扩展，程序一启动就被杀掉（`status=4/ILL`，日志里是
> `This binary was compiled with aes enabled, but this feature is not available`）。
> Pi 5 没有这个问题。0.10.x 能跑，但它的 aarch64 包最高只到 Python 3.12，而新系统自带
> 3.13。`deploy/pi4_mediapipe_fix.sh` 一步到位：用 uv 装一个独立的 Python 3.12，
> 逐个试 0.10.x，挑第一个能在本机加载模型的版本重建虚拟环境（实机选中 0.10.18）。
>
> 上面的路径按用户名 `pi` 写的；用 Raspberry Pi Imager 自定义了用户名的话，
> 路径和 `acepoint.service` 里的 `User=` 都要跟着改。

### 更新代码

```bash
cd ~/piggy-acepoint && git pull && bash deploy/pi_update.sh
```

`pi_update.sh` 把仓库里的 `src/`、`scripts/`、`deploy/` 拷进运行目录 `~/AcePoint`，
给 `config.pi.toml` 补上新增的配置项（已有的、现场改过的都不动），停服务跑一遍
`--check`，再启动。日志在 `~/pi-update.log`。

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

### 屏完全不亮：绕过 luma 的底层自检

MAX7219 上电默认 shutdown，全黑不代表坏。`scripts/led_raw_test.py` 直接写
display-test 寄存器强制全亮，不依赖其他任何寄存器，能把硬件故障和软件故障分开：

```bash
sudo systemctl stop acepoint
python scripts/led_raw_test.py               # CS 接 26 脚时加 --device 1
```

想每次开机都自检（无屏无键盘时目视判断），装一次即可：

```bash
sudo cp deploy/acepoint-ledtest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable acepoint-ledtest
```

开机顺序：全亮 5 秒 → 棋盘格 5 秒 → 熄灭 → `acepoint` 起来后显示 `00 00`。
全亮都没有就是硬件问题（供电、DIN/DOUT 接反、CS 脚、电平）。自检只排在
`acepoint` 之前、不被它依赖，失败不影响计分服务；结果看
`journalctl -u acepoint-ledtest -b`。排查完可 `sudo systemctl disable acepoint-ledtest`。

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
> 装盒前请把 `config.pi.toml` 里的 `device` 改成稳定路径（字符串）：
>
> ```bash
> ls -l /dev/v4l/by-id/       # 按序列号：usb-openaicam_openaicam_2505121128-video-index0
> ls -l /dev/v4l/by-path/     # 按 USB 口：...-usb-0:1.1:1.0-video-index0
> ```
>
> 微雪 OV2735 每只有独立序列号，优先用 `by-id`：换 USB 口也不会左右颠倒。
> 没有序列号的摄像头才用 `by-path`，这时插口就不能再换。
>
> ```toml
> [[sources]]
> side = "left"
> device = "/dev/v4l/by-id/usb-openaicam_openaicam_2505121128-video-index0"
> ```
>
> 路径不存在（没插、序列号写错）时只停用这一半场，另一半照常计分，日志会写
> `camera ... not found`。

## 5. 跑一次实测标定

### 5.1 采样率

**所有防误触参数都是在开发机上按 5fps/路 标定的**，Pi 的算力能不能维持这个
采样率必须实测。开发机单帧推理约 21ms；**Pi 4 实测 157~219ms**，左右轮流识别时
每路只有 2.5fps —— 0.6 秒的手势触发率从 100% 掉到 50%。所以 `config.pi.toml`
里 `recognition.parallel` 和 `capture.decode_on_demand` 必须开着（需求 §6 v1.3）。

每路的实际帧率和单帧识别耗时看网页面板的"帧率 / 识别耗时"，或者：

```bash
curl -s http://localhost:8080/api/state | python3 -m json.tool | grep -E '"fps"|recognize_ms'
python scripts/check_sampling.py -c config.pi.toml      # 按实测采样率复核相位
```

`parallel` + `decode_on_demand` + `inference_threads = 2` 都开着时，实机实测：
没人比手势每路 4.9~5.0fps，有人比手势的那几秒约 3.8~4.2fps（画面里有手时识别最慢）。
偏离这组数字就先查配置项是不是丢了、CPU 温度（`vcgencmd measure_temp`，
无散热时 77°C，加风扇 54°C）和有没有降频（`vcgencmd get_throttled` 应为 `0x0`）。

每路 ≥4fps 时现有参数够用（≥0.5 秒的手势 100% 触发）。低于 4fps 的话
`hold_seconds` / `hold_grace_seconds` / `hold_min_samples` 都要按新的采样周期
重新标定（需求 §10 阶段 2）。

### 5.2 识别区

识别区必须在**真实机位**上标定：设备装在网柱顶，人站到 2~3 米处按平时的习惯比手势。
桌面上的取景和球场完全不同，标出来的结果搬不过去。

```bash
sudo systemctl stop acepoint
python scripts/record_camera.py -c config.pi.toml left       # 录 30 秒，右边同理
python scripts/compare_roi.py -c config.pi.toml videos/calib-left.avi \
    --ratios 0.60x0.80 0.40x0.30@0.50x0.35 0.30x0.25@0.50x0.35
sudo systemctl start acepoint
```

**先扫中心再扫大小**（需求 §5.2）。两路取景不同就在各自的 `[[sources]]` 里分别配
`crop_w_ratio` / `crop_h_ratio` / `crop_cx_ratio` / `crop_cy_ratio`。

## 6. 开机自启

### 可选语音播报

安装 `pip install -e ".[pi,audio]"`，在服务实际使用的用户和音频输出设备上运行
`acepoint --audio-check -c config.pi.toml`、`acepoint --audio-test -c config.pi.toml`。
确认有声且音量合适后，再开启配置中的 `[audio].enabled`。3.5mm接口需接有源音箱。
systemd运行环境与桌面登录环境的默认声卡／权限可能不同，必须实际验证；缺设备时音频降级，计分继续。
增加音箱后重新测整机功耗及续航。网页调音需要启用web后端，详见[语音使用说明](../docs/语音播报使用说明.md)。

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

设备没有关机键，球场上就是直接拔充电宝，所以装好、调好之后必须开。树莓派要能联网
（第一次开启会 `apt install overlayroot`），在家里做，别等到球场。

```bash
# 顺序不能反：启动分区只读要改 /etc/fstab，只读系统生效后就改不了了
sudo raspi-config nonint enable_bootro && sudo raspi-config nonint enable_overlayfs && sudo reboot
findmnt -no FSTYPE /            # 重启后应为 overlay
```

等价的菜单操作是 `sudo raspi-config` -> Performance Options -> Overlay File System，
两个问题都选 Yes。原理是内核参数 `overlayroot=tmpfs`：SD 卡上的根分区只读挂载，
所有写入落在内存里的上层。

**开启后的任何改动重启就没了** —— 程序更新、`config.pi.toml`、录的标定素材都是。
`pi_update.sh` 检测到只读会提醒。要让改动留下来，走一遍维护流程：

```bash
sudo raspi-config nonint disable_overlayfs && sudo reboot     # 1. 关只读
cd ~/piggy-acepoint && git pull && bash deploy/pi_update.sh   # 2. 更新 / 改配置 / 标定
sudo raspi-config nonint enable_overlayfs && sudo reboot      # 3. 开只读
```

（启动分区的只读不用动：`disable_overlayfs` 会自己临时把它挂成可写去改 `cmdline.txt`。）
反过来这也是个安全网：只读开着时先跑一遍更新当试运行，不满意重启就恢复原样。

需求 §3.3.4 要求拔电即关机不损坏 TF 卡。开启后注意两点：

- **写入不会失败，而是写进内存里的 overlay 上层，掉电即失**。所以
  `journalctl` 的日志每次断电全部丢失——"第三局中间突然不计分了"这类问题
  事后无从查起。这是这台无网设备唯一的可观测性缺口。
- 别在只读系统上加文件日志：它会在内存里无限增长，和 MediaPipe 的约 352MB
  一起挤 2GB 内存，最后 OOM。同时确认 `dphys-swapfile` 已关闭。
  （现在这台是 8GB 的 Pi 4，交换分区是 zram，不占 SD 卡；换成 2GB 的板子要重新核对。）

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
