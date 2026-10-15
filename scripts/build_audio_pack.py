"""Build the bundled offline Chinese demo pack using Windows System.Speech.

Run on Windows: python scripts/build_audio_pack.py
Runtime playback on Linux uses the generated WAVs and needs no speech engine.
"""
import array
import json
import math
from pathlib import Path
import subprocess
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1] / "audio"
LINES = {
    "streak_3": ["三连击！偷偷练了？", "三连击！手感来了！", "三连击！藏得挺深！"],
    "streak_5": ["五连击！检查球拍！", "五连击！拦不住了！", "五连击！有点东西！"],
    "streak_7": ["超神！今天你买单！", "超神！还让不让人玩！", "超神！球拍开光了！"],
    "shutdown": ["终结！该我了！", "终结！装完了吧！", "终结！终于逮到你！"],
    "comeback_tie": ["追平！刚才没开机！", "追平！谁说要走？", "追平！好戏来了！"],
    "comeback_lead": ["反超！你还当真了？", "反超！轮到我笑了！", "反超！惊不惊喜？"],
    "icebreaker": ["破蛋！庆祝一下！", "开张！终于有了！", "破蛋！给点掌声！"],
    "deuce": ["二十平！加班开始！", "二十平！谁也别走！", "二十平！继续加班！"],
    "decider": ["最后一球！赢了吹一年！", "一球定胜负！别手抖！", "决胜球！主角请就位！"],
    "match_point": ["赛点！嘴先闭上！", "赛点！手可别抖！", "赛点！稳住别浪！"],
    "win_normal": ["拿下！可以发群里了！", "胜利！今晚你最靓！", "拿下！掌声在哪里！"],
    "win_comeback": ["逆风翻盘！刚才谁笑最大声？", "逆转拿下！剧本都不敢这么写！", "翻盘成功！这波可以吹一年！"],
    "win_dominant": ["比赛结束！友情是否结束，另行通知！", "拿下！这手感，建议续费！", "大胜！今天这顿，你请客！"],
    "banter": ["这球可以！", "有点东西！", "别飘，继续！", "好球，再来！", "稳住，能赢！"],
    "point": [""],
}
CLEAN = {"streak_3": "三连得分！", "streak_5": "五连得分！", "streak_7": "七连得分！",
         "shutdown": "精彩反击！", "comeback_tie": "追平比分！", "comeback_lead": "成功反超！",
         "icebreaker": "拿下第一分！", "deuce": "二十平！继续加油！", "decider": "决胜一分！",
         "match_point": "赛点，加油！", "win_normal": "恭喜获胜！", "win_comeback": "逆转获胜！",
         "win_dominant": "精彩表现，恭喜获胜！", "banter": "好球，加油！", "point": ""}


def tone(event):
    notes = [660, 880, 1100] if event.startswith("streak") else [880, 660]
    if event.startswith("win"):
        notes = [523, 659, 784, 1047]
    if event == "point":
        notes = [880]
    data = array.array("h")
    for hz in notes:
        for i in range(3307):
            envelope = math.sin(math.pi * i / 3307) ** 2
            data.append(int(5500 * envelope * math.sin(2 * math.pi * hz * i / 44100)))
    return data


def main():
    ROOT.mkdir(exist_ok=True)
    jobs = []
    for style in ("friends", "clean", "effects"):
        (ROOT / style).mkdir(exist_ok=True)
        for event, lines in LINES.items():
            for index, line in enumerate(lines if style == "friends" else [CLEAN[event] if style == "clean" else ""]):
                jobs.append({"id": f"{style}_{event}_{index}", "style": style, "event": event,
                             "text": line, "file": f"{style}/{event}_{index}.wav",
                             "source": "Windows System.Speech demo voice; procedural original tones"})
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        (temp / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8-sig")
        script = r'''
param([string]$TaskRoot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SelectVoice('Microsoft Huihui Desktop')
$speaker.Rate = 3
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(44100, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$jobs = Get-Content -Encoding UTF8 (Join-Path $TaskRoot 'jobs.json') | ConvertFrom-Json
foreach ($job in $jobs) {
    if ($job.text) {
        $speaker.SetOutputToWaveFile((Join-Path $TaskRoot ($job.id + '.wav')), $format)
        $speaker.Speak($job.text)
        $speaker.SetOutputToNull()
    }
}
$speaker.Dispose()
'''
        (temp / "speak.ps1").write_text(script, encoding="utf-8-sig")
        subprocess.run(["powershell", "-NoProfile", "-File", str(temp / "speak.ps1"), str(temp)], check=True)
        for job in jobs:
            data = tone(job["event"])
            if job["text"]:
                with wave.open(str(temp / (job["id"] + ".wav")), "rb") as wav:
                    speech = array.array("h", wav.readframes(wav.getnframes()))
                # Remove synthesis padding without changing spoken speed.
                audible = [i for i, v in enumerate(speech) if abs(v) > 100]
                if audible:
                    speech = speech[max(0, audible[0]-441):min(len(speech), audible[-1]+2205)]
                peak = max((abs(v) for v in speech), default=1)
                data.extend(int(v * min(1.5, 18000 / peak)) for v in speech)
            duration = len(data) / 44100
            limit = 4 if job["event"].startswith("win") else 2.5
            if duration > limit:
                raise ValueError(f"Rewrite long clip: {job['id']} {duration:.2f}s")
            with wave.open(str(ROOT / job["file"]), "wb") as wav:
                wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                wav.writeframes(data.tobytes())
            job["duration"] = round(duration, 3)
    (ROOT / "manifest.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Built {len(jobs)} clips in {ROOT}")


if __name__ == "__main__":
    main()

