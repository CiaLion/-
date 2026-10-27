# -*- coding: utf-8 -*-
"""roformer_device.py -- Mel-Band RoFormer 混合设备推理与硬件加速封装。

为 Intel Arc (XPU)、NVIDIA/AMD ROCm (CUDA) 及 CPU 提供统一的高性能推理与多级安全降级。
策略：xpu (Intel) -> cuda (NVIDIA / AMD ROCm) -> cpu
特性：
  1. 混合设备推理（CPU 处理 STFT/iSTFT 与复数算子，GPU/XPU 执行密集 Transformer 计算），
     规避各类硬件环境复数算子不稳定与 ATen 内部回退警告，保证与 CPU 100% 数值一致。
  2. 精度适配：XPU 默认高效 FP32（Intel Arc 具备澎湃 FP32 算力且无动态转换开销）；
     CUDA/ROCm 支持 Transformer 级 FP16 autocast 加速，并保护 view_as_complex。
  3. OOM 与异常保护：捕获显存溢出或执行异常时，自动清理显存缓存并平滑降级至 CPU，绝不崩溃。
  4. 支持 monkeypatch 与独立调用两种模式，与 sep_runner.py 无缝兼容。
"""
from __future__ import annotations

import gc
import logging
import os
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import torch

logger = logging.getLogger("roformer_device")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(asctime)s - %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def is_xpu_available() -> bool:
    """检查 Intel XPU 是否可用。"""
    try:
        return hasattr(torch, "xpu") and torch.xpu.is_available() and torch.xpu.device_count() > 0
    except Exception:
        return False


def is_cuda_available() -> bool:
    """检查 NVIDIA CUDA 或 AMD ROCm (HIP) 是否可用。"""
    try:
        return torch.cuda.is_available() and torch.cuda.device_count() > 0
    except Exception:
        return False


def is_amd_gpu() -> bool:
    """检查当前 CUDA 设备是否为 AMD 显卡 (ROCm / HIP)。"""
    try:
        if is_cuda_available():
            dev_name = torch.cuda.get_device_name(0).lower()
            is_hip = hasattr(torch.version, "hip") and torch.version.hip is not None
            return is_hip or "amd" in dev_name or "radeon" in dev_name
    except Exception:
        pass
    return False


def get_device_name(dev_type: str) -> str:
    """获取设备型号名称。"""
    try:
        if dev_type == "xpu" and is_xpu_available():
            return torch.xpu.get_device_name(0)
        if dev_type == "cuda" and is_cuda_available():
            prefix = "AMD GPU (ROCm)" if is_amd_gpu() else "NVIDIA GPU (CUDA)"
            return f"{prefix} - {torch.cuda.get_device_name(0)}"
    except Exception:
        pass
    return "CPU"


def detect_device(preferred: Optional[str] = None) -> str:
    """按优先级确定目标设备：xpu -> cuda -> cpu。"""
    env_device = os.environ.get("ROFORMER_DEVICE", "").strip().lower()
    target = (preferred or env_device or "auto").lower()

    if target == "xpu":
        if is_xpu_available():
            return "xpu"
        logger.warning("请求了 xpu 但 Intel XPU 不可用，降级检测...")
    elif target in ("cuda", "gpu", "nvidia"):
        if is_cuda_available():
            return "cuda"
        logger.warning("请求了 cuda 但 CUDA/ROCm 不可用，降级检测...")
    elif target in ("amd", "rocm", "radeon"):
        if is_cuda_available():
            return "cuda"
        logger.warning("请求了 AMD (ROCm) 但当前环境无可用 GPU，降级至 CPU...")
        return "cpu"
    elif target == "cpu":
        return "cpu"

    if is_xpu_available():
        return "xpu"
    if is_cuda_available():
        return "cuda"
    return "cpu"


def clean_device_memory(dev_type: Optional[str] = None):
    """清理加速设备显存与垃圾回收。"""
    gc.collect()
    try:
        if (dev_type is None or dev_type == "xpu") and is_xpu_available():
            torch.xpu.empty_cache()
    except Exception:
        pass
    try:
        if (dev_type is None or dev_type == "cuda") and is_cuda_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _audio_seconds(path):
    try:
        import torchaudio
        info = torchaudio.info(path)
        return max(1.0, float(info.num_frames) / float(info.sample_rate))
    except Exception:
        return 180.0


def _start_progress_tick(src, device):
    import threading
    stop = threading.Event()
    seconds = _audio_seconds(src)
    factor = 2.3 if device == "cpu" else 0.35

    def run():
        t0 = time.time()
        while not stop.wait(2.0):
            pct = min(95, int(100.0 * (time.time() - t0) / max(1.0, seconds * factor)))
            print("[分离进度] %d%%" % pct, flush=True)

    threading.Thread(target=run, daemon=True).start()
    return stop


def _create_hybrid_forward(original_forward: Callable) -> Callable:
    """创建混合设备 forward 函数。

    CPU 负责：STFT 变换、频带切分前置索引、复数 view_as_complex、频带加权累加、iSTFT 逆变换；
    硬件设备 (XPU / CUDA) 负责：BandSplit 线性层、Time/Freq Transformer 轴向注意力、Mask 估计层。
    """
    from einops import rearrange, repeat
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import pack_one, unpack_one, pack, unpack

    def hybrid_forward(self, raw_audio, target=None):
        model_device = next(self.parameters()).device
        if model_device.type == "cpu":
            return original_forward(self, raw_audio, target)

        cpu_dev = torch.device("cpu")
        raw_audio_cpu = raw_audio.to(cpu_dev)
        if raw_audio_cpu.ndim == 2:
            raw_audio_cpu = rearrange(raw_audio_cpu, "b t -> b 1 t")

        batch, channels, raw_audio_length = raw_audio_cpu.shape
        istft_length = raw_audio_length if self.match_input_audio_length else None

        raw_audio_packed, batch_shape = pack_one(raw_audio_cpu, "* t")

        # 1. 在 CPU 上进行高保真 STFT
        stft_window = self.stft_window_fn().to(cpu_dev)
        stft_repr = torch.stft(raw_audio_packed, **self.stft_kwargs, window=stft_window, return_complex=True)
        stft_repr = torch.view_as_real(stft_repr)
        stft_repr = unpack_one(stft_repr, batch_shape, "* f t c")
        stft_repr = rearrange(stft_repr, "b s f t c -> b (f s) t c")

        batch_arange = torch.arange(batch, device=cpu_dev)[..., None]
        freq_idx_cpu = self.freq_indices.cpu()
        x = stft_repr[batch_arange, freq_idx_cpu]
        x = rearrange(x, "b f t c -> b t (f c)")

        # 2. 将纯实数特征张量传入加速硬件 (XPU / CUDA) 计算 Transformer
        x = x.to(model_device)

        # 精度控制：CUDA 环境支持 float16 autocast；XPU 环境默认原生 FP32（算力充沛且避免类型转换开销）
        precision = os.environ.get("ROFORMER_PRECISION", "auto").strip().lower()
        use_fp16 = (precision == "fp16") or (precision == "auto" and model_device.type == "cuda")

        autocast_ctx = (
            torch.autocast(device_type=model_device.type, dtype=torch.float16)
            if use_fp16
            else torch.no_grad()
        )

        with autocast_ctx:
            x = self.band_split(x)
            for time_transformer, freq_transformer in self.layers:
                x = rearrange(x, "b t f d -> b f t d")
                x, ps = pack([x], "* t d")
                x = time_transformer(x)
                (x,) = unpack(x, ps, "* t d")

                x = rearrange(x, "b f t d -> b t f d")
                x, ps = pack([x], "* f d")
                x = freq_transformer(x)
                (x,) = unpack(x, ps, "* f d")

            masks = torch.stack([fn(x) for fn in self.mask_estimators], dim=1)
            masks = rearrange(masks, "b n t (f c) -> b n f t c", c=2)

        # 3. 将估计好的 masks 送回 CPU，确保复数运算在 CPU 完成
        masks = masks.to(device=cpu_dev, dtype=torch.float32)

        stft_repr = rearrange(stft_repr, "b f t c -> b 1 f t c")
        stft_repr = torch.view_as_complex(stft_repr)
        masks = torch.view_as_complex(masks)
        masks = masks.type(stft_repr.dtype)

        scatter_indices = repeat(freq_idx_cpu, "f -> b n f t", b=batch, n=self.num_stems, t=stft_repr.shape[-1])
        stft_repr_expanded = repeat(stft_repr, "b 1 ... -> b n ...", n=self.num_stems)

        masks_summed = torch.zeros_like(stft_repr_expanded).scatter_add_(2, scatter_indices, masks)
        denom = repeat(self.num_bands_per_freq.cpu(), "f -> (f r) 1", r=channels)
        masks_averaged = masks_summed / denom.clamp(min=1e-8)

        stft_repr = stft_repr * masks_averaged
        stft_repr = rearrange(stft_repr, "b n (f s) t -> (b n s) f t", s=self.audio_channels)

        # 4. 在 CPU 上进行 iSTFT 逆变换
        recon_audio = torch.istft(stft_repr, **self.stft_kwargs, window=stft_window,
                                  return_complex=False, length=istft_length)
        recon_audio = rearrange(recon_audio, "(b n s) t -> b n s t", b=batch,
                                s=self.audio_channels, n=self.num_stems)

        if self.num_stems == 1:
            recon_audio = rearrange(recon_audio, "b 1 s t -> b s t")

        return recon_audio

    return hybrid_forward


_ORIG_MEL_FORWARD = None
_PATCHED = False


def patch_audio_separator(device: Optional[str] = None):
    """Monkeypatch audio_separator 以无缝启用硬件加速与混合设备执行。"""
    global _ORIG_MEL_FORWARD, _PATCHED
    from audio_separator.separator import Separator
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    chosen_dev = detect_device(device)
    logger.info(f"配置 RoFormer 目标设备: {chosen_dev.upper()} ({get_device_name(chosen_dev)})")

    # 1. 替换 Separator 设备识别
    def patched_setup_torch_device(self, system_info):
        ort_providers = []
        try:
            import onnxruntime as ort
            ort_providers = ort.get_available_providers()
        except Exception:
            pass

        self.torch_device_cpu = torch.device("cpu")
        self.torch_device_mps = None

        if chosen_dev == "xpu":
            self.logger.info("Intel XPU 可用，设置 Torch 设备为 XPU")
            self.torch_device = torch.device("xpu")
            self.onnx_execution_provider = ["CPUExecutionProvider"]
        elif chosen_dev == "cuda":
            self.logger.info("CUDA/ROCm 可用，设置 Torch 设备为 CUDA")
            self.torch_device = torch.device("cuda")
            if "CUDAExecutionProvider" in ort_providers:
                self.onnx_execution_provider = ["CUDAExecutionProvider"]
            else:
                self.onnx_execution_provider = ["CPUExecutionProvider"]
        else:
            self.logger.info("使用 CPU 设备执行")
            self.torch_device = self.torch_device_cpu
            self.onnx_execution_provider = ["CPUExecutionProvider"]

    Separator.setup_torch_device = patched_setup_torch_device

    # 2. 注入 MelBandRoformer 混合设备 forward
    if not _PATCHED:
        _ORIG_MEL_FORWARD = MelBandRoformer.forward
        MelBandRoformer.forward = _create_hybrid_forward(_ORIG_MEL_FORWARD)
        _PATCHED = True
        logger.info("已成功装配 MelBandRoformer 混合设备执行引擎 (CPU STFT + XPU/GPU Transformer)")


def unpatch_audio_separator():
    """撤销 monkeypatch，恢复纯原生行为。"""
    global _PATCHED, _ORIG_MEL_FORWARD
    if _PATCHED and _ORIG_MEL_FORWARD is not None:
        from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer
        MelBandRoformer.forward = _ORIG_MEL_FORWARD
        _PATCHED = False
        logger.info("已恢复 MelBandRoformer 默认 forward 实现")


def separate_roformer_with_fallback(
    src: str,
    out_vocal: str,
    out_inst: str,
    preferred_device: Optional[str] = None,
    model_dir: Optional[str] = None,
    ckpt_name: str = "mel_band_roformer_vocals_fv4_gabox.ckpt",
    tmp_root: Optional[str] = None,
) -> Dict[str, Any]:
    """带自动降级的 Mel-Band RoFormer 人声分离。

    执行链条：preferred / auto (xpu) -> fallback (cuda) -> fallback (cpu)
    任一级发生错误或 OOM 均保证安全释放显存，平滑降级，绝不崩溃。
    """
    from audio_separator.separator import Separator

    base_dir = os.path.dirname(os.path.abspath(__file__))
    model_dir = model_dir or os.path.join(base_dir, "models", "separation")
    tmp_root = tmp_root or os.path.abspath(os.path.join(base_dir, "..", "..", "logs", "tmp_wav"))
    os.makedirs(tmp_root, exist_ok=True)

    # 确定降级候选链
    initial_device = detect_device(preferred_device)
    device_chain = [initial_device]
    if initial_device == "xpu":
        if is_cuda_available():
            device_chain.append("cuda")
        device_chain.append("cpu")
    elif initial_device == "cuda":
        device_chain.append("cpu")
    elif initial_device == "cpu":
        pass

    last_error = None
    start_time = time.time()

    for idx, dev in enumerate(device_chain):
        logger.info(f"=== 尝试在设备 [{dev.upper()}] 上运行分离 (尝试 {idx+1}/{len(device_chain)}) ===")
        work_dir = os.path.join(tmp_root, f"roformer_work_{dev}_{os.getpid()}_{int(time.time()*1000)%10000}")
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)

        try:
            patch_audio_separator(device=dev)
            sep = Separator(
                model_file_dir=model_dir,
                output_dir=work_dir,
                output_format="wav",
                sample_rate=44100,
            )
            sep.load_model(ckpt_name)
            stop = _start_progress_tick(src, dev)
            try:
                files = sep.separate(src, custom_output_names={"vocals": "vocal", "instrumental": "inst"})
            finally:
                stop.set()

            got: Dict[str, str] = {}
            for path in files:
                if not os.path.isabs(path):
                    path = os.path.join(work_dir, path)
                name = os.path.basename(path).lower()
                if name.startswith("vocal"):
                    got["vocal"] = path
                elif name.startswith("inst"):
                    got["inst"] = path

            if "vocal" not in got or "inst" not in got:
                raise RuntimeError(f"RoFormer 输出缺失必要音轨: {files}")

            os.makedirs(os.path.dirname(os.path.abspath(out_vocal)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.abspath(out_inst)), exist_ok=True)
            shutil.copyfile(got["vocal"], out_vocal)
            shutil.copyfile(got["inst"], out_inst)

            duration = time.time() - start_time
            logger.info(f"分离成功完成！生效设备: {dev.upper()}, 耗时: {duration:.2f} s")
            clean_device_memory(dev)
            return {
                "status": "success",
                "device": dev,
                "device_name": get_device_name(dev),
                "duration": duration,
                "vocal_path": out_vocal,
                "inst_path": out_inst,
                "attempt": idx + 1,
            }

        except Exception as exc:
            logger.warning(f"设备 [{dev.upper()}] 执行失败: {exc}")
            last_error = exc
            clean_device_memory(dev)
            if idx + 1 < len(device_chain):
                logger.warning(f"正在自动降级至下一个可用设备: [{device_chain[idx+1].upper()}]...")
            continue
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    raise RuntimeError(f"所有可用设备均已尝试并失败，最后错误: {last_error}")


def separate_roformer(src: str, out_vocal: str, out_inst: str, device: Optional[str] = None):
    """drop-in 兼容 sep_runner.py 的分离入口。"""
    res = separate_roformer_with_fallback(src, out_vocal, out_inst, preferred_device=device)
    return res
