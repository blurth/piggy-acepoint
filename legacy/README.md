# legacy

`badminton_pi4.py` 是重构前的单文件原型（需求文档 §10 阶段 0/1）。

保留它只为对照，**不再维护**，功能已经全部搬到 `src/acepoint/`：

| 原型里的东西 | 现在在哪 |
|---|---|
| `GestureFSM` | `acepoint/fsm.py` |
| `open_camera` / `crop_roi` | `acepoint/sources/camera.py`、`acepoint/engine.py` |
| `led_worker` | `acepoint/displays/led.py` |
| `--no-led` 终端输出 | `acepoint/displays/console.py` |
| `--preview` 的 cv2 窗口 | 网页记分牌 `acepoint/displays/web.py` |
| 文件顶部的配置常量 | `config.toml` / `config.pi.toml` |
