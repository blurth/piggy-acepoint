# AcePoint · 羽毛球手势计分系统

单柱自供电设备盒：树莓派 4 + 双 USB 摄像头 + LED 点阵屏 + 充电宝。
球员得分后走到网前对镜头比"耶"保持 1 秒，本半场自动 +1，屏上实时刷新比分。
完整需求见 [`羽毛球手势计分系统-需求文档.md`](羽毛球手势计分系统-需求文档.md)。

语音功能：[开发方案](docs/语音气氛播报开发方案.md) · [使用与验收说明](docs/语音播报使用说明.md)。已支持离线连得分、关键比分和获胜播报。

**当前阶段**：开发联调 —— 用两段**录制视频**替代摄像头，用**网页记分牌**替代 LED 点阵屏。
两者都是可插拔后端，上实机时只换 `config.pi.toml`，业务代码一行不用改。

## 快速开始（Windows / 开发机）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

python scripts/fetch_model.py          # 下载手势模型（约 8MB，只需一次）
```

把两段测试视频放进 `videos/`：`A.mp4` = 左半场，`B.mp4` = 右半场。
（暂时没有素材可以先跑 `python scripts/make_sample_videos.py` 生成占位视频，
它只验证链路通不通，画面里没有真人手势，不会加分。）

```powershell
acepoint                               # 或 python -m acepoint
```

浏览器打开 <http://localhost:8080> 看记分牌。

## 常用命令

```powershell
acepoint --check                       # 自检：模型在不在 / 视频源开不开得了 / LED 依赖装没装
acepoint --video left=videos/A.mp4 --video right=videos/B.mp4
acepoint --display console --duration 20        # 无界面冒烟测试，跑 20 秒退出
acepoint --port 8081 --no-preview               # 换端口 / 关掉画面预览
acepoint -v                                     # DEBUG 日志

pytest                                          # 跑测试
pytest tests/test_fsm.py::test_cooldown_blocks_everything   # 跑单个用例
```

## 调参脚本

换素材之后（尤其是阶段 2 换成网柱横屏实拍）参数要重新标定，这几个脚本直接跑就出表，
它们复用项目自己的 `crop_roi` / `GestureFSM` / 配置，量出来的就是实际会跑的东西：

```powershell
python scripts/analyze_gestures.py videos/B.mp4 --sweep   # 手势时长标定 + hold/grace 扫描
python scripts/compare_roi.py videos/B.mp4                # 识别区裁多窄还认得出来
python scripts/compare_recognizer.py videos/B.mp4         # num_hands / per_source 的取舍
python scripts/profile_pipeline.py --resolution           # 耗时分布 + 内存 + 分辨率曲线
python scripts/check_sampling.py                          # 采样相位扫描（改 poll_hz/hold 后必跑）
python scripts/explain_moment.py videos/B.mp4 14          # 某个手势为什么没计分
```

`explain_moment.py` 能一眼分清四种「没反应」：根本没识别到 / 置信度不够 / 保持时长不够 /
被冷却挡掉。最后一种在连续摆拍的素材上最常见——真实打球一个回合远超冷却期，不必当问题。

不给视频参数就用配置里的视频源；`-c config.pi.toml` 可以按实机配置来量。

## 网页记分牌

`http://localhost:8080`，替代 LED 点阵屏用：

- 顶部大比分，计分瞬间闪烁 2 次（对应需求 §5.3 的 LED 反馈）
- 每个半场一块面板：实时画面 + 识别区框、当前手势、保持进度条、冷却倒计时、帧率
- `+1 / -1 / 清零` 按钮，方便在没有真人做手势时验证显示链路
- 手机上看：把 `config.toml` 里 `display.web.host` 改成 `"0.0.0.0"`，然后访问开发机 IP

## 树莓派实机

摄像头已选定 **微雪 OV2735 2MP USB Camera (A)**（SKU 33122），同型号两只。
板子尺寸 25 × 25mm，支持 1080P 30fps MJPEG，便于背对背固定在网柱设备盒内。
该型号的双路兼容性及球馆识别效果待实测；采购、安装和验证要求见[需求文档 §2.1](羽毛球手势计分系统-需求文档.md)。

```bash
sudo raspi-config          # Interface Options -> SPI -> Enable
python3 -m venv ~/scorer && source ~/scorer/bin/activate
pip install -e ".[pi]"
python scripts/fetch_model.py

ls /dev/video*             # 确认摄像头设备号，写进 config.pi.toml
acepoint -c config.pi.toml --check
acepoint -c config.pi.toml
```

开机自启（systemd）：

```ini
# /etc/systemd/system/scorer.service
[Unit]
Description=AcePoint Badminton Scorer
After=multi-user.target

[Service]
User=pi
WorkingDirectory=/home/pi/AcePoint
ExecStart=/home/pi/scorer/bin/acepoint -c /home/pi/AcePoint/config.pi.toml
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now scorer
journalctl -u scorer -f
```

## 手势指令（MediaPipe 预置，无需训练）

| 手势 | 保持时长    | 动作 |
|---|---------|---|
| Victory（比耶） | ≥ 0.5 秒 | 识别到的半场 +1 |
| Pointing_Up（食指向上） | ≥ 0.5 秒 | 识别到的半场 +1（和比耶等价，比哪个都行） |
| Thumb_Down（拇指向下） | ≥ 0.5 秒 | 识别到的半场 -1，不低于 0 |
| Open_Palm（张开手掌） | ≥ 3.0 秒 | 双方清零 |

## 赛制（单局 21 分）

先到 21 分且领先 ≥2 分获胜；20:20 之后先领先 2 分者胜；**29:29 之后先得第 30 分者直接获胜**（封顶）。

**赛点**（再得 1 分就获胜）会实时标出来：网页上比分下方出现「赛点」标签，LED 上该侧底部
画一条下划线。29:29 时双方同时是赛点。

分出胜负后本局冻结：加分被忽略（防庆祝时误触），**撤销仍可用**（最后一分误加时能撤回），
张开手掌 3 秒清零开下一局。LED 上获胜方的数字持续闪烁，网页上弹出获胜横幅。

不做自动归零——计分后还有冷却期，比自动归零窗口更长，一旦自动清掉就再也撤不回来。
规则参数在 `[game]` 段，`enabled = false` 可退化成纯计分器。

只记录单局：不做三局两胜、不做换边、不做断电续分。**比分绑定球场半场，换边时需先清零。**

## 防误触

持续确认 + 计分后该半场冷却 5 秒 + 置信度阈值 0.6 + 只处理画面中央识别区。
参数都在 `config.toml` / `config.pi.toml` 里，对应需求文档 §7。

`hold_seconds = 0.5` 是用真人自然做手势的录像标定的：实际保持 0.5~1.6 秒（中位 0.67 秒），
5fps 采样下一次手势只落到 3~4 个采样点。配套的 `hold_grace_seconds = 0.4` 允许保持期间短暂
丢检而不重新计时——识别在 5fps 下难免闪断，不留容忍窗口的话掉一帧计时就归零，
实测成功率 45% vs 91%。

## 项目结构

```
config.toml            开发配置（录制视频 + 网页）
config.pi.toml         实机配置（双摄像头 + LED 点阵）
src/acepoint/
  cli.py               命令行入口与环境自检
  config.py            TOML -> dataclass，需求 §7 的参数都在这
  engine.py            主循环：取帧 -> 裁识别区 -> 识别 -> 状态机 -> 比分 -> 显示
  fsm.py               手势状态机（持续确认 + 冷却），纯逻辑
  recognition.py       MediaPipe 封装（时间戳单调、置信度过滤）
  state.py             线程安全的比分板与遥测数据
  sources/             视频源：camera.py（USB 摄像头）/ video.py（录制视频）
  displays/            显示后端：web.py（网页）/ led.py（MAX7219）/ console.py
scripts/               模型下载、占位视频生成
tests/                 状态机 / 配置 / 比分板 / 视频源
legacy/                重构前的单文件原型，仅供对照
```
