"""SheetSage2 Headless CLI（供 pipeline.py 调用的无界面入口）。

使用示例:
  python transcribe_cli.py <音频> --output <输出目录> --device xpu --dtype bf16
"""

import argparse
import os
import shutil
import sys
import warnings
from pathlib import Path

# Intel XPU 上少数算子会回退到 CPU（例如 STFT），只是一次性提示，不必吓用户
warnings.filterwarnings("ignore", message=r".*Aten Op fallback.*")

# 添加当前目录与 SheetSage2 模型目录至模块搜索路径
WEBUI_ROOT = Path(__file__).resolve().parent
SHEETSAGE2_DIR = WEBUI_ROOT / "models" / "SheetSage2"
MERT_DIR = WEBUI_ROOT / "models" / "MERT-v2-FullSong"

# ---- 项目内 HuggingFace 缓存隔离（必须在 import transformers 之前）----
# 绝不读写 C:\Users\<用户>\.cache\huggingface：复制/改名/换盘后仍然自包含。
LOCAL_HF_CACHE = WEBUI_ROOT / ".hf_cache"
os.environ["HF_HOME"] = str(LOCAL_HF_CACHE)
os.environ["HF_MODULES_CACHE"] = str(LOCAL_HF_CACHE / "modules")
os.environ["TORCH_HOME"] = str(WEBUI_ROOT / ".torch_cache")
os.environ["NUMBA_CACHE_DIR"] = str(WEBUI_ROOT / ".numba_cache")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")

sys.path.insert(0, str(SHEETSAGE2_DIR))
sys.path.insert(0, str(WEBUI_ROOT))

# 1. 屏蔽 Windows 系统级 DLL 找不到入口点的模态弹窗
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

import torch

# 2. 拦截 torchaudio 非必需 C++ 扩展 (.pyd) 版本错位崩溃，并注入空模块防止下一行 import 报错
try:
    _orig_load_lib = torch.ops.load_library

    def _safe_load_lib(path):
        try:
            return _orig_load_lib(path)
        except OSError:
            if "torchaudio" in str(path).lower():
                import types
                m = types.ModuleType("torchaudio.lib._torchaudio")
                m.cuda_version = lambda: None
                sys.modules.setdefault("torchaudio.lib", types.ModuleType("torchaudio.lib"))
                sys.modules["torchaudio.lib._torchaudio"] = m
                return False
            raise

    torch.ops.load_library = _safe_load_lib
except Exception:
    pass

# 3. 自动挂载 imageio_ffmpeg 内置解码器，防止纯净电脑无系统 ffmpeg 无法读取 .m4a/.aac
try:
    import imageio_ffmpeg
    _ff_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if _ff_exe and os.path.isfile(_ff_exe):
        _ff_dir = os.path.dirname(_ff_exe)
        os.environ["PATH"] = _ff_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass
from transformers import AutoConfig, AutoModel

from hardware_router import select_attention, select_dtype


def _copy_if_changed(src, dst):
    """Copy src over dst when content could differ (size OR mtime)."""
    try:
        if dst.is_file():
            s = src.stat()
            d = dst.stat()
            if s.st_size == d.st_size and s.st_mtime_ns == d.st_mtime_ns:
                return False
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def sync_local_model_code():
    """把 models 下的补丁版 .py 同步进项目内 modules 缓存。

    transformers 的 trust_remote_code 会优先使用缓存里的旧代码；这里每次
    覆盖一遍，确保增量解码等补丁不会被旧缓存盖掉。

    另外再叠一层项目补丁（vendor/sheetsage2/patched_model_py）：模型仓库
    里原样是官方代码，只有 bootstrap 恢复补丁时才会覆盖进 models 目录；
    用户如果只换了程序没重装，缓存里就还是官方版。
    这里让补丁在每次启动时都生效，不依赖是否重装过。
    """
    copied = 0
    patch_dir = WEBUI_ROOT / "patched_model_py"
    for name, src_dir in (("SheetSage2", SHEETSAGE2_DIR),
                          ("MERT-v2-FullSong", MERT_DIR)):
        if not src_dir.is_dir():
            continue
        target = LOCAL_HF_CACHE / "modules" / "transformers_modules" / name
        target.mkdir(parents=True, exist_ok=True)
        for src in src_dir.glob("*.py"):
            if _copy_if_changed(src, target / src.name):
                copied += 1
        # project patches always win over the shipped repo code
        if name == "SheetSage2" and patch_dir.is_dir():
            for src in patch_dir.glob("*.py"):
                # (a) the models dir itself: trust_remote_code may import it directly
                if _copy_if_changed(src, src_dir / src.name):
                    copied += 1
                # (b) the project module cache
                if _copy_if_changed(src, target / src.name):
                    copied += 1
    return copied


def clean_system_hf_cache():
    """清掉系统 C 盘里可能被污染的旧模块缓存（一次性）。"""
    removed = False
    base = Path.home() / ".cache" / "huggingface" / "modules" / "transformers_modules"
    for child in base.glob("*") if base.is_dir() else []:
        # 精确匹配本项目用到的两个模型目录，避免误删用户的其它 MERT 变体
        if child.name in ("SheetSage2", "MERT-v2-FullSong"):
            shutil.rmtree(child, ignore_errors=True)
            removed = True
    return removed


def make_progress_printer():
    """把模型的分窗口进度转成一行一条的日志，供上层解析。"""
    last = {"window": 0, "pct": -1}

    def report(event):
        stage = event.get("stage")
        window = int(event.get("window") or 0)
        windows = int(event.get("windows") or 0)
        if not window or not windows:
            return
        if stage == "encoding":
            pct = int(round((window - 1) * 100.0 / windows))
        elif stage == "window_complete":
            pct = int(round(window * 100.0 / windows))
        else:
            return
        if pct <= last["pct"] and stage != "window_complete":
            return
        last["pct"] = max(last["pct"], pct)
        last["window"] = window
        print(f"[进度] 窗口 {window}/{windows}（{pct}%）", flush=True)

    return report


def main():
    parser = argparse.ArgumentParser(description="SheetSage2 Headless CLI")
    parser.add_argument("audio", type=Path, help="输入音频路径 (wav, mp3, flac 等)")
    parser.add_argument("--output", "-o", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--device", "-d", default="xpu",
                        choices=["auto", "xpu", "cuda", "amd", "rocm", "cpu"],
                        help="推理设备")
    parser.add_argument("--dtype", default="bf16",
                        choices=["bf16", "fp16", "fp32"], help="推理精度")
    parser.add_argument("--max-seconds", type=float, default=None, help="最大转录时长 (秒)，默认全曲")
    args = parser.parse_args()

    audio_path = args.audio.resolve()
    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not audio_path.exists():
        print(f"[错误] 输入音频文件不存在: {audio_path}")
        sys.exit(1)

    print("=" * 65)
    print(f" SheetSage2 CLI 启动 (目标设备: {args.device}, 精度: {args.dtype})")
    print(f" 输入文件: {audio_path}")
    print(f" 输出目录: {out_dir}")
    print("=" * 65)

    req_device = str(args.device).strip().lower()
    if req_device in ("auto", ""):
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            device = "xpu"
        elif torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"
    elif req_device in ("amd", "rocm"):
        if torch.cuda.is_available():
            device = "cuda"
            is_hip = hasattr(torch.version, "hip") and torch.version.hip is not None
            dname = ""
            try:
                dname = torch.cuda.get_device_name(0)
            except Exception:
                pass
            print(f"[提示] 已挂载 AMD ROCm/CUDA 设备: {dname or 'AMD GPU'}")
        else:
            print("[警告] 请求了 AMD (ROCm) 设备，但当前 PyTorch 未检测到可用 GPU，自动安全回退至 CPU 推理")
            device = "cpu"
    elif req_device == "xpu":
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            print("[警告] XPU 不可用，自动安全回退至 CPU 推理")
            device = "cpu"
        else:
            device = "xpu"
    elif req_device == "cuda":
        if not torch.cuda.is_available():
            print("[警告] CUDA/ROCm 不可用，自动安全回退至 CPU 推理")
            device = "cpu"
        else:
            device = "cuda"
    else:
        device = "cpu"
    # ---- 硬件动态路由：注意力与精度都不写死成某一家显卡 ----
    device_kind = device
    cuda_major = None
    bf16_ok = None
    if device_kind == "cuda":
        try:
            cuda_major = int(torch.cuda.get_device_capability(0)[0])
        except Exception:
            cuda_major = None
        try:
            bf16_ok = bool(torch.cuda.is_bf16_supported())
        except Exception:
            bf16_ok = None
    elif device_kind == "xpu":
        try:
            bf16_ok = bool(torch.xpu.is_bf16_supported())
        except Exception:
            bf16_ok = None
    attn = select_attention(device_kind, cuda_major)

    # 精度防崩：老 N 卡 / CPU 自动降级
    requested = str(args.dtype).lower()
    if requested == "bf16":
        routed = select_dtype(device_kind, cuda_major, bf16_ok)
        if routed != "bf16":
            print("[提示] %s 上 bf16 不可用/过慢，自动改用 %s" % (device_kind.upper(), routed))
        dtype = routed
    elif requested == "fp16":
        if device_kind == "cpu":
            print("[提示] CPU 下 fp16 不可用，自动降级为 fp32")
            dtype = "fp32"
        else:
            dtype = "fp16"
    else:
        dtype = "fp32"

    if device_kind == "cpu":
        try:
            n = max(4, min(10, int((os.cpu_count() or 8) * 0.65)))
            torch.set_num_threads(n)
            print("[提示] CPU 推理线程数：%d" % n)
        except Exception:
            pass

    print("      硬件路由：%s  attention=%s  dtype=%s" % (device_kind, attn, dtype), flush=True)
    print("      HF 缓存：%s" % LOCAL_HF_CACHE, flush=True)
    copied = sync_local_model_code()
    if copied:
        print("      已同步 %d 个模型代码文件到项目内缓存" % copied, flush=True)
    if clean_system_hf_cache():
        print("      已清理硬盘上旧的 transformers_modules\\SheetSage2 缓存", flush=True)

    sheetsage_path = str(SHEETSAGE2_DIR).replace("\\", "/")
    mert_path = str(MERT_DIR).replace("\\", "/")
    print("[1/3] 正在加载 SheetSage2 模型与 MERT 主干权重...")
    model_config = AutoConfig.from_pretrained(
        sheetsage_path,
        trust_remote_code=True,
        local_files_only=True,
    )
    model_config.decoder_attn_implementation = attn
    model = AutoModel.from_pretrained(
        sheetsage_path,
        config=model_config,
        base_model_path=mert_path,
        trust_remote_code=True,
        local_files_only=True,
    ).eval().to(device)
    print(f"      模型已加载到 {device}（{dtype}）", flush=True)

    # 验证实际生效的注意力实现（不只是改 config 字段）
    try:
        layer0 = model.decoder.layers[0]
        actual = layer0.self_attn.__class__.__name__
        print("      注意力实现：%s（%s）" % (attn, actual), flush=True)
    except Exception:
        print("      注意力实现：%s" % attn, flush=True)

    print("[2/3] 开始转录音频...")
    options = {
        "dtype": dtype,
        "max_seconds": args.max_seconds,
    }

    try:
        result = model.transcribe(str(audio_path), output_dir=out_dir,
                                  progress=make_progress_printer(), **options)
    except Exception as e:
        if device != "cpu":
            print(f"[警告] 设备 [{device.upper()}] 推理异常 ({e})，正在自动安全回退至 CPU 重新执行...")
            try:
                del model
            except Exception:
                pass
            import gc
            gc.collect()
            try:
                if device == "cuda" and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                elif device == "xpu" and hasattr(torch, "xpu") and torch.xpu.is_available():
                    torch.xpu.empty_cache()
            except Exception:
                pass

            cpu_model = AutoModel.from_pretrained(
                sheetsage_path,
                config=model_config,
                base_model_path=mert_path,
                trust_remote_code=True,
                local_files_only=True,
            ).eval().to("cpu")
            options["dtype"] = "fp32"
            result = cpu_model.transcribe(str(audio_path), output_dir=out_dir,
                                          progress=make_progress_printer(), **options)
        else:
            print(f"[错误] 转录过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    print("[3/3] 转录完成！已生成核心资产:")
    expected_files = [
        "melody_vocal.mid",
        "melody_instrumental.mid",
        "chords.mid",
        "transcription.mid",
        "events.json"
    ]
    for ef in expected_files:
        fp = out_dir / ef
        if fp.exists():
            print(f"  - {ef:26s} ({fp.stat().st_size} bytes)")
        else:
            print(f"  - [未找到] {ef}")

    print("=" * 65)
    print(f"全部完成，输出目录: {out_dir}")


if __name__ == "__main__":
    main()
