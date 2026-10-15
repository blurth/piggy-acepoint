# 离线气氛音频包

当前包共75个PCM WAV片段（44.1kHz、16bit、单声道），约9.5MB。
`friends` 为熟人互损，`clean` 为文明鼓励，`effects` 为纯音效。
每种熟人事件有3句（普通插话5句），普通得分只有短音效。

中文为本机 Windows System.Speech 的 Microsoft Huihui Desktop 合成初版，
音效为项目脚本生成的音调。它是可实际播放的试听版本，音色尚不是专业电竞配音。
后续可用真人录音替换同名WAV并更新 `manifest.json`；无需修改计分逻辑。

```powershell
python scripts/check_audio_pack.py
acepoint --audio-test
```

在安装了对应中文声音的Windows上，可用 `python scripts/build_audio_pack.py` 重新生成。
运行AcePoint时无需Windows语音引擎或联网；Linux直接播放这些WAV。
普通片段最多2.5秒，获胜片段最多4秒，替换后运行校验脚本。
清单记录片段文字、事件、风格、文件及生成来源；自行更换素材时保留来源记录。
