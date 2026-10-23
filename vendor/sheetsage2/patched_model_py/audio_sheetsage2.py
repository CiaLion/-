"""File decoding and waveform preparation for music transcription."""
from pathlib import Path
import io
import math
import numbers
import os
import shutil
import subprocess

import numpy as np
import torch

SAMPLE_RATE = 24000
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ffmpeg_exe():
    """PATH 里的 ffmpeg 优先；没有就用 imageio-ffmpeg 自带的那一份。

    Windows 环境下没有系统 ffmpeg 时，MP3/M4A 会直接报
    "FFmpeg is required"；imageio-ffmpeg 的 wheel 内含 ffmpeg 可执行文件，
    免安装、免 PATH，所有显卡机器都适用。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _encoded_bytes(audio):
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return bytes(audio)
    if callable(getattr(audio, "read", None)):
        position = None
        try:
            position = audio.tell()
            audio.seek(0)
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            position = None
        try:
            value = audio.read()
        finally:
            if position is not None:
                audio.seek(position)
        if not isinstance(value, (bytes, bytearray)):
            raise ValueError("Audio streams must return encoded audio bytes")
        return bytes(value)
    return None


def load_audio(audio, *, sampling_rate=None, max_seconds=None, preset="default"):
    if max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError("max_seconds must be finite and positive")
    encoded = _encoded_bytes(audio)
    if encoded is not None and not encoded:
        raise ValueError("Encoded audio must not be empty")
    if isinstance(audio, (str, Path)) or encoded is not None:
        if preset == "paper":
            import torchaudio
            source = io.BytesIO(encoded) if encoded is not None else str(audio)
            info = torchaudio.info(source, backend="ffmpeg")
            if info.num_frames <= 0:
                raise ValueError("Cannot determine source audio length")
            if encoded is not None:
                source.seek(0)
            waveform, rate = torchaudio.load(source, frame_offset=0, num_frames=int(info.num_frames),
                                            backend="ffmpeg", channels_first=True)
            waveform = waveform.mean(dim=0)
            if rate != SAMPLE_RATE:
                waveform = torchaudio.functional.resample(waveform, rate, SAMPLE_RATE)
        else:
            ffmpeg_exe = _ffmpeg_exe()
            if not ffmpeg_exe:
                raise RuntimeError(
                    "FFmpeg is required to read audio files; install imageio-ffmpeg "
                    "(pip install imageio-ffmpeg) or add ffmpeg to PATH")
            source = "pipe:0" if encoded is not None else str(Path(audio).resolve())
            command = [ffmpeg_exe, "-v", "error", "-nostdin", "-i", source, "-vn"]
            if max_seconds is not None:
                command += ["-t", str(float(max_seconds))]
            command += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"]
            result = subprocess.run(command, input=encoded, capture_output=True, timeout=600,
                                    check=False, creationflags=_NO_WINDOW)
            if result.returncode:
                raise ValueError("Cannot decode audio: " + result.stderr.decode(errors="replace")[-1200:])
            waveform = torch.from_numpy(np.frombuffer(result.stdout, dtype="<f4").copy())
    else:
        if not isinstance(sampling_rate, numbers.Real) or not math.isfinite(sampling_rate) or sampling_rate <= 0 or sampling_rate != int(sampling_rate):
            raise ValueError("Provide sampling_rate for an array or tensor waveform")
        waveform = torch.as_tensor(audio, dtype=torch.float32, device="cpu")
        if waveform.ndim == 2:
            if waveform.shape[0] > 32:
                raise ValueError("Multichannel audio must have shape [channels, samples]")
            waveform = waveform.mean(dim=0)
        if waveform.ndim != 1:
            raise ValueError("Audio must have shape [samples] or [channels, samples]")
        if sampling_rate != SAMPLE_RATE:
            import torchaudio
            waveform = torchaudio.functional.resample(waveform, int(sampling_rate), SAMPLE_RATE)
    waveform = waveform.float().contiguous()
    if max_seconds is not None:
        waveform = waveform[:round(max_seconds * SAMPLE_RATE)]
    if waveform.numel() < 1025 or not torch.isfinite(waveform).all():
        raise ValueError("Audio must contain at least 1025 finite samples at 24 kHz")
    return waveform


def slice_audio(audio, start, seconds):
    offset, count = round(start * SAMPLE_RATE), round(seconds * SAMPLE_RATE)
    result = audio[offset:offset + count]
    return torch.nn.functional.pad(result, (0, count - len(result)))
