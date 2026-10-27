# -*- coding: utf-8 -*-
"""sep_runner.py -- 在 vendor/sheetsage2/.venv 内运行的原生音源分离。

用法：
  python sep_runner.py --model htdemucs --input X --out-vocal A.wav --out-inst B.wav

依赖见同目录 requirements-sep.txt（RoFormer 依赖 audio-separator==0.30.2）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import torch

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP_DIR = os.path.join(ROOT, "models", "separation")
os.makedirs(SEP_DIR, exist_ok=True)
TORCH_CACHE = os.path.join(ROOT, ".torch_cache")
os.environ.setdefault("TORCH_HOME", TORCH_CACHE)
os.environ.setdefault("HF_HOME", os.path.join(ROOT, ".hf_cache"))
os.makedirs(os.path.join(TORCH_CACHE, "hub", "checkpoints"), exist_ok=True)

SOURCES = ["drums", "bass", "other", "vocals"]
HDEMUCS_WEIGHT = os.path.join(SEP_DIR, "hdemucs_high.th")
ROFORMER_CKPT = "mel_band_roformer_vocals_fv4_gabox.ckpt"
ROFORMER_YAML = "config_mel_band_roformer_vocals_gabox.yaml"
TMP_DIR = os.path.abspath(os.path.join(ROOT, "..", "..", "logs", "tmp_wav"))
try:
    os.makedirs(TMP_DIR, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = TMP_DIR
except Exception:
    pass

WEIGHT_SOURCES = {
    "hdemucs_high.th": [
        "https://hf-mirror.com/paobukaidecha/hdemucs_high_trained/resolve/main/hdemucs_high_trained.pt",
        "https://ghfast.top/https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt",
        "https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt",
    ],
    ROFORMER_CKPT: [
        "https://hf-mirror.com/GaboxR67/MelBandRoformers/resolve/main/melbandroformers/vocals/voc_fv4.ckpt",
        "https://ghfast.top/https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/mel_band_roformer_vocals_fv4_gabox.ckpt",
        "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/mel_band_roformer_vocals_fv4_gabox.ckpt",
    ],
    ROFORMER_YAML: [
        "https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/config_mel_band_roformer_vocals_gabox.yaml",
        "https://hf-mirror.com/GaboxR67/MelBandRoformers/resolve/main/melbandroformers/vocals/voc_gabox.yaml",
    ],
}
WEIGHT_MIN_MB = {"hdemucs_high.th": 250, ROFORMER_CKPT: 700, ROFORMER_YAML: 0}


def _progress(msg):
    print("[分离进度] " + msg, flush=True)


def _fetch_url(url, part, min_bytes):
    resume = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"User-Agent": "Mozilla/5.0"}
    if resume:
        headers["Range"] = "bytes=%d-" % resume
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resume and getattr(resp, "status", 200) != 206:
            resume = 0
        total = resume + int(resp.headers.get("Content-Length") or 0)
        mode = "ab" if resume else "wb"
        got = resume
        started = last = time.time()
        mark = got
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last >= 0.5:
                    last = now
                    pct = int(100.0 * got / total) if total else 0
                    _progress("正在下载分离模型: %.1f / %.1f MB (%d%%)" % (
                        got / 1048576.0, total / 1048576.0, pct))
                if now - started >= 8.0 and (got - mark) / max(1e-6, now - started) < 30 * 1024:
                    raise RuntimeError("下载速度过慢")
    size = os.path.getsize(part) if os.path.isfile(part) else 0
    if size < min_bytes:
        raise RuntimeError("文件不完整")
    if total and size < total:
        raise RuntimeError("下载中断")


def ensure_weight(name):
    dest = os.path.join(SEP_DIR, name)
    min_bytes = int(WEIGHT_MIN_MB.get(name, 0) * 1048576)
    if os.path.isfile(dest) and os.path.getsize(dest) >= min_bytes:
        return dest
    part = dest + ".part"
    last = None
    for url in WEIGHT_SOURCES.get(name, []):
        try:
            _progress("正在下载分离模型: 连接 %s" % url.split("/")[2])
            _fetch_url(url, part, min_bytes)
            os.replace(part, dest)
            _progress("分离模型就绪: %s" % name)
            return dest
        except Exception as exc:  # noqa: BLE001
            last = exc
            _progress("镜像不可用，换下一个（%s）" % str(exc)[:60])
    raise RuntimeError("分离模型下载失败：%s（%s）" % (name, last))


def ensure_audio_separator():
    try:
        import audio_separator  # noqa: F401
        return True
    except Exception:
        pass
    req = os.path.join(ROOT, "requirements-sep.txt")
    for index in ("https://pypi.tuna.tsinghua.edu.cn/simple",
                  "https://mirrors.aliyun.com/pypi/simple",
                  "https://mirrors.cloud.tencent.com/pypi/simple"):
        _progress("正在补齐分离依赖: %s" % index.split("/")[2])
        code = subprocess.call(
            [sys.executable, "-m", "pip", "install", "-r", req, "-i", index,
             "--timeout", "30", "--disable-pip-version-check", "-q"])
        if code == 0:
            return True
    return False


def _pick_device():
    try:
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            return "xpu"
    except Exception:
        pass
    try:
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_audio(path):
    import torchaudio
    try:
        wav, sr = torchaudio.load(path)
    except Exception:
        import subprocess
        import tempfile
        ff = None
        try:
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ff = None
        if not ff:
            raise
        tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "logs", "tmp_wav")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp = tempfile.mkstemp(suffix=".wav", dir=tmp_dir)[1]
        subprocess.run([ff, "-y", "-i", path, "-ac", "2", "-ar", "44100", tmp],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wav, sr = torchaudio.load(tmp)
        try:
            os.remove(tmp)
        except Exception:
            pass
    if wav.dim() == 1:
        wav = wav.unsqueeze(0)
    if wav.size(0) == 1:
        wav = wav.repeat(2, 1)
    return wav[:2], sr


def _load_state(path):
    try:
        return torch.load(path, map_location="cpu", weights_only=True)
    except Exception:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            return torch.load(path, map_location="cpu", weights_only=False)


def _htdemucs_model():
    from torchaudio.models import hdemucs_high
    legacy = os.path.join(TORCH_CACHE, "hub", "torchaudio", "models",
                          "hdemucs_high_trained.pt")
    if not os.path.isfile(HDEMUCS_WEIGHT) and os.path.isfile(legacy):
        try:
            shutil.copyfile(legacy, HDEMUCS_WEIGHT)
        except Exception:
            pass
    weight = ensure_weight("hdemucs_high.th")
    model = hdemucs_high(sources=SOURCES)
    model.load_state_dict(_load_state(weight))
    model.eval()
    return model


def separate_htdemucs(wav, sr, chunk_sec=30, device=None):
    model = _htdemucs_model()
    dev = device or _pick_device()
    try:
        model = model.to(dev)
    except Exception:
        dev = "cpu"
        model = model.to(dev)
    wav = wav.to(dev)
    total = wav.shape[1]
    chunk = int(chunk_sec * sr)
    hop = max(1, chunk - sr)
    acc = torch.zeros(2, total, device=dev)
    weight = torch.zeros(1, total, device=dev)
    win = torch.hann_window(chunk, periodic=False, device=dev)
    start = 0
    while start < total:
        seg = wav[:, start:start + chunk]
        n = seg.shape[1]
        if n < chunk:
            seg = torch.nn.functional.pad(seg, (0, chunk - n))
        with torch.no_grad():
            out = model(seg.unsqueeze(0))
        sources = out[0] if out.dim() == 4 else out
        vocals = sources[3, :, :n]
        w = win[:n].unsqueeze(0)
        acc[:, start:start + n] += vocals * w
        weight[:, start:start + n] += w
        start += hop
        _progress("%d%%" % min(99, int(100.0 * start / max(1, total))))
    vocals = acc / torch.clamp(weight, min=1e-6)
    inst = wav - vocals
    return vocals.cpu(), inst.cpu()


def separate_roformer(src, out_vocal, out_inst, device=None):
    ensure_weight(ROFORMER_YAML)
    ensure_weight(ROFORMER_CKPT)
    if not ensure_audio_separator():
        raise RuntimeError("分离依赖安装失败，请检查网络后重试")
    try:
        import roformer_device  # type: ignore
    except Exception:
        roformer_device = None
    if roformer_device is not None:
        roformer_device.separate_roformer_with_fallback(
            src, out_vocal, out_inst, preferred_device=device,
            model_dir=SEP_DIR, ckpt_name=ROFORMER_CKPT, tmp_root=TMP_DIR)
        return
    try:
        from audio_separator.separator import Separator  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "未安装 Mel-Band RoFormer 推理包（audio-separator）；"
            "权重: %s；原始错误: %s" % (os.path.join(SEP_DIR, ROFORMER_CKPT), exc))
    work = os.path.join(TMP_DIR, "roformer_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        sep = Separator(model_file_dir=SEP_DIR, output_dir=work,
                        output_format="wav", sample_rate=44100)
        sep.load_model(ROFORMER_CKPT)
        files = sep.separate(src, custom_output_names={"vocals": "vocal",
                                                       "instrumental": "inst"})
        got = {}
        for path in files:
            if not os.path.isabs(path):
                path = os.path.join(work, path)
            name = os.path.basename(path).lower()
            if name.startswith("vocal"):
                got["vocal"] = path
            elif name.startswith("inst"):
                got["inst"] = path
        if "vocal" not in got or "inst" not in got:
            raise RuntimeError("RoFormer 输出缺少人声/伴奏：%s" % (files,))
        os.makedirs(os.path.dirname(os.path.abspath(out_vocal)), exist_ok=True)
        shutil.copyfile(got["vocal"], out_vocal)
        shutil.copyfile(got["inst"], out_inst)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _save(path, tensor, sr):
    import torchaudio
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    torchaudio.save(path, torch.clamp(tensor, -1.0, 1.0), sr,
                    encoding="PCM_S", bits_per_sample=16)


def _rms_of(path):
    import torchaudio
    wav, _sr = torchaudio.load(path)
    return float(torch.sqrt(torch.mean(wav ** 2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="htdemucs", choices=("htdemucs", "roformer"))
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-vocal", required=True)
    ap.add_argument("--out-inst", required=True)
    ap.add_argument("--chunk", type=float, default=30.0)
    ap.add_argument("--device", default="")
    args = ap.parse_args()

    if args.model == "roformer":
        try:
            separate_roformer(args.input, args.out_vocal, args.out_inst,
                              device=args.device or None)
            voc = _rms_of(args.out_vocal)
            src = _rms_of(args.input)
            if voc < max(1e-4, src * 0.02):
                raise RuntimeError("人声轨几乎无声（%.5f）" % voc)
            print("SEP_OK " + json.dumps({"model": "roformer", "vocal_rms": round(voc, 4)},
                                         ensure_ascii=False))
            return
        except Exception as exc:  # noqa: BLE001
            print("ROFORMER_FALLBACK %s" % exc, flush=True)
    wav, sr = _load_audio(args.input)
    vocal, inst = separate_htdemucs(wav, sr, chunk_sec=args.chunk,
                                    device=args.device or None)
    _save(args.out_vocal, vocal, sr)
    _save(args.out_inst, inst, sr)
    print("SEP_OK " + json.dumps({"sr": sr, "seconds": round(wav.shape[1] / sr, 2),
                                  "device": args.device or "auto"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
