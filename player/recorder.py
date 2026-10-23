# -*- coding: utf-8 -*-
"""recorder.py -- 演奏时同步录音（录"电脑正在放的声音"，不是话筒）。

════════════════════════════════════════════════════════════════════════════
用途：程序一按下开始，就把系统输出（也就是游戏发出的声音）录下来；
      **正常弹完 → 留下录音；中途手动停（F8 再按一次 / F12）→ 直接作废删掉。**

原理：Windows 的 WASAPI 支持"环回录音"（loopback）——把某个**输出设备**正在
     播放的声音原样录下来，不需要话筒、不需要插线。用 `soundcard` 这个库调用。

两条实测踩过的坑（2026-10-17）：
  1. **采样率必须跟设备的默认采样率一致**，否则某些实现会**静默返回全 0**；
     所以这里一律用 `speaker.default_samplerate`，不硬写 48000。
  2. 用别的播放器（如 winsound）放音时，声音可能并不经过你正在录的那个设备；
     真演奏时是游戏在放，**要确保录的就是游戏实际输出的那个设备**（界面上可选）。

写盘策略：边录边写（int16 PCM），先写 `.part` 临时文件；
  正常结束 → 改名成正式文件；中途取消 → 删掉 `.part`。
  这样即使程序崩了，也不会留下一个"看起来像录音"的坏文件。
"""
import os
import shutil
import subprocess
import threading
import time
import wave

try:
    import numpy as np
except Exception:                     # 理论上不会缺；缺了也只是不能录，别影响演奏
    np = None

# ★ `soundcard` **故意不在这里 import**：它一被调用就会把**当前线程**的 COM
#   初始化成"多线程套间（MTA）"，而 Qt 的系统文件对话框要求"单线程套间（STA）"。
#   一旦在界面线程里碰过它，点「打开文件」就会卡死（实测 RPC_E_CHANGED_MODE）。
#   所以：凡是能用它的地方，都必须是**后台线程**，并且 import 也推迟到那里。
_SC = None
_SC_TRIED = False

def _sc():
    """后台线程里才拿 soundcard；返回 None 表示这个库用不了。"""
    global _SC, _SC_TRIED
    if not _SC_TRIED:
        _SC_TRIED = True
        try:
            import soundcard as _mod
            _SC = _mod
        except Exception:
            _SC = None
    return _SC

def list_speakers():
    """所有可录的输出设备名。**只能在后台线程里调**（见文件头 COM 说明）。"""
    sc = _sc()
    if sc is None:
        return []
    try:
        return [s.name for s in sc.all_speakers()]
    except Exception:
        return []

def default_speaker_name():
    """默认输出设备名。**只能在后台线程里调**。"""
    sc = _sc()
    if sc is None:
        return None
    try:
        return sc.default_speaker().name
    except Exception:
        return None

class LoopbackRecorder(object):
    """环回录音器：`start()` 开录，`stop(keep=True/False)` 收尾。"""

    def __init__(self, path, log=None, device_name=None, block_sec=0.10):
        self.path = path
        self.log = log or (lambda m: None)
        self.device_name = device_name
        self.block_sec = block_sec
        self.samplerate = 48000
        self.channels = 2
        self.error = None
        self.recorded = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._part = path + ".part"
        self._paused = False
        self._pause_lock = threading.Lock()
        self.written_bytes = 0

    @property
    def paused(self):
        with self._pause_lock:
            return self._paused

    def pause(self):
        """暂停写入（仍继续读音频块，清空底层缓冲，防止积压）。"""
        with self._pause_lock:
            self._paused = True

    def resume(self):
        with self._pause_lock:
            self._paused = False

    def start(self):
        """立刻开始录（在后台线程里）。返回 True/False，**失败绝不抛异常**。"""
        if np is None:
            self.error = "没装 numpy，无法录音"
            self.log("⚠ %s（演奏照常进行）" % self.error)
            return False
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
        except Exception as exc:
            self.error = "建目录失败：%s" % exc
            self.log("⚠ %s" % self.error)
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _pick_device(self, sc):
        spks = sc.all_speakers()
        if not spks:
            raise RuntimeError("系统里找不到输出设备")
        if self.device_name:
            for s in spks:
                if s.name == self.device_name:
                    return s
        return sc.default_speaker()

    def _run(self):
        try:
            sc = _sc()                     # ★ 在这个后台线程里才碰 soundcard / COM
            if sc is None:
                self.error = "没装 soundcard 库，无法录音"
                self.log("⚠ %s（演奏照常进行）" % self.error)
                return
            spk = self._pick_device(sc)
            # ★ 用设备自己的默认采样率（不一致会静默录成 0）
            sr = int(getattr(spk, "default_samplerate", 0) or 0) or 48000
            self.samplerate = sr
            mic = sc.get_microphone(id=str(spk.id), include_loopback=True)
            self.log("录音开始：%s（%d Hz，环回）" % (spk.name, sr))
            frames = max(1, int(sr * self.block_sec))
            t0 = time.perf_counter()
            with wave.open(self._part, "wb") as w:
                w.setnchannels(self.channels)
                w.setsampwidth(2)
                w.setframerate(sr)
                with mic.recorder(samplerate=sr, channels=self.channels) as rec:
                    while not self._stop.is_set():
                        block = rec.record(numframes=frames)
                        with self._pause_lock:
                            if self._paused:
                                continue      # 只读不写：把硬件缓冲读空，绝不写入静音
                            pcm = (np.clip(block, -1.0, 1.0) * 32767).astype("<i2")
                            data = pcm.tobytes()
                            w.writeframes(data)
                            self.written_bytes += len(data)
            self.recorded = round(time.perf_counter() - t0, 2)
        except Exception as exc:
            self.error = str(exc)
            self.log("⚠ 录音中断：%s（演奏不受影响）" % exc)

    def stop(self, keep=True):
        """停止录音。`keep=True` 保留成正式文件，`False` 直接删掉。**

        返回最终文件路径（作废时返回 None）。
        保留时会把 wav **压成 mp3**（控制在 5MB 以内），wav 原文件删掉。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self.error and not os.path.exists(self._part):
            return None
        try:
            if keep and os.path.exists(self._part):
                if os.path.exists(self.path):
                    os.remove(self.path)
                # ★ 中间文件必须**以 .wav 结尾**：录制写出来的是 WAV，
                #   而 mp3 的目标名就是 self.path。若先把 wav 改成 .mp3 再压，
                #   ffmpeg 的输入和输出会变成同一个文件 → 必然失败，
                #   结果留下一个"扩展名写着 mp3、其实是 48MB WAV"的假文件
                tmp_wav = os.path.splitext(self.path)[0] + ".wav"
                os.replace(self._part, tmp_wav)
                size = os.path.getsize(tmp_wav)
                if size < 2000:                      # 只有文件头 = 什么都没录到
                    self.log("⚠ 录音文件几乎是空的（%d 字节），可能录错了设备" % size)
                    os.replace(tmp_wav, self.path)
                    return self.path
                out = _to_mp3(tmp_wav, self.log)
                if out:
                    return out
                # 压缩失败：老老实实留成 .wav（别用 mp3 当扩展名骗人）
                os.replace(tmp_wav, os.path.splitext(self.path)[0] + ".wav")
                return os.path.splitext(self.path)[0] + ".wav"
            if os.path.exists(self._part):
                os.remove(self._part)                # 手动中断 → 作废
        except Exception as exc:
            self.log("⚠ 录音收尾失败：%s" % exc)
        return None

# ══════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════
FIXED_KBPS = 320        # 固定码率，不按长度自适应
# 参考大小：320kbps ≈ 每分钟 2.3MB（4 分钟约 9.4MB）

def _ffmpeg():
    """找 ffmpeg：先看 PATH，再看 winget 装的那个位置。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                        "Packages")
    if os.path.isdir(cand):
        for root, dirs, files in os.walk(cand):
            if "ffmpeg.exe" in files and os.sep + "bin" in root:
                return os.path.join(root, "ffmpeg.exe")
    return None

def _wav_seconds(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0

def _to_mp3(wav_path, log=None):
    """把 wav 压成 mp3，成功则删掉 wav 并返回 mp3 路径；失败返回 None（保留 wav）。"""
    log = log or (lambda m: None)
    if os.path.splitext(wav_path)[1].lower() == ".mp3":
        log("⚠ 输入文件名是 .mp3，跳过压缩（避免 ffmpeg 输入输出同名）")
        return None
    ff = _ffmpeg()
    if not ff:
        log("⚠ 找不到 ffmpeg，录音保留成 wav（偏大）")
        return None
    secs = _wav_seconds(wav_path)
    if secs <= 0:
        return None
    kbps = FIXED_KBPS
    mp3 = os.path.splitext(wav_path)[0] + ".mp3"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-i", wav_path, "-codec:a", "libmp3lame",
           "-b:a", "%dk" % kbps, "-ac", "2", mp3]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and os.path.exists(mp3) and os.path.getsize(mp3) > 1000:
            mb = os.path.getsize(mp3) / 1024.0 / 1024.0
            log("录音已压缩：%d kbps，%.1f MB（原 wav %.1f 分钟）"
                % (kbps, mb, secs / 60.0))
            try:
                os.remove(wav_path)
            except Exception:
                pass
            return mp3
        log("⚠ 压缩失败（ffmpeg 返回 %s），保留 wav" % r.returncode)
    except Exception as exc:
        log("⚠ 压缩失败：%s（保留 wav）" % exc)
    return None

def make_path(base_dir, song_name, role_cn):
    """录音文件名：录音\\歌名-角色-20261017-1630.mp3"""
    safe = "".join(c for c in (song_name or "未命名")
                   if c not in '\\/:*?"<>|').strip() or "未命名"
    safe = os.path.splitext(safe)[0][:40]
    role = "".join(c for c in (role_cn or "") if c not in '\\/:*?"<>|（）() ')
    stamp = time.strftime("%Y%m%d-%H%M")
    return os.path.join(base_dir, "%s-%s-%s.mp3" % (safe, role, stamp))
