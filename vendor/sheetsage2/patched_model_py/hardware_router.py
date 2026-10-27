"""Cross-vendor inference routing for SheetSage2.

The goal is one model code path that is fast on every machine and never
assumes a particular GPU vendor:

* attention:  CUDA sm_80+ -> "sdpa"; XPU / CPU / older CUDA / AMD -> "eager"
* precision:  XPU -> bf16; CUDA/AMD (bf16 capable) -> bf16; old CUDA/AMD -> fp16;
              CPU -> fp32

Only the pure decision functions live here (no torch import at module level)
so the release gate can unit-test every branch on any host.
"""


def normalize_device_type(device_type):
    """Map torch device types / user labels to the routing buckets."""
    value = str(device_type or "").strip().lower()
    if value in ("cuda", "gpu", "nvidia", "amd", "rocm", "radeon"):
        return "cuda"
    if value in ("xpu", "intel"):
        return "xpu"
    return "cpu"


def select_attention(device_type, cuda_major=None):
    """Pick the decoder attention implementation for this hardware.

    ``cuda_major`` is ``torch.cuda.get_device_capability()[0]`` when known.
    Unknown capability is treated as new enough only when the device is not
    CUDA; CUDA without a probed capability falls back to eager (safe).
    """
    kind = normalize_device_type(device_type)
    if kind == "cuda" and cuda_major is not None and int(cuda_major) >= 8:
        return "sdpa"
    return "eager"


def select_dtype(device_type, cuda_major=None, bf16_supported=None):
    """Pick the autocast precision for this hardware.

    ``bf16_supported`` comes from ``torch.cuda.is_bf16_supported()``. When it
    is unknown, sm_80+ is assumed to support bf16 (true for Ampere and newer).
    """
    kind = normalize_device_type(device_type)
    if kind == "xpu":
        if bf16_supported is False:
            return "fp16"
        return "bf16"
    if kind == "cuda":
        if bf16_supported is None:
            bf16_supported = cuda_major is not None and int(cuda_major) >= 8
        return "bf16" if bf16_supported else "fp16"
    return "fp32"


def detect_routing():
    """Probe the current process and return a routing dict.

    Returns ``{device_type, cuda_major, bf16_supported, vendor, attention, dtype}``.
    Safe to call on machines without torch (returns CPU routing).
    """
    device_type, cuda_major, bf16_supported = "cpu", None, None
    vendor = "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            device_type = "cuda"
            is_hip = hasattr(torch.version, "hip") and torch.version.hip is not None
            dev_name = ""
            try:
                dev_name = torch.cuda.get_device_name(0).lower()
            except Exception:
                pass
            if is_hip or "amd" in dev_name or "radeon" in dev_name:
                vendor = "amd"
            else:
                vendor = "nvidia"

            try:
                cuda_major = int(torch.cuda.get_device_capability(0)[0])
            except Exception:
                cuda_major = None
            try:
                bf16_supported = bool(torch.cuda.is_bf16_supported())
            except Exception:
                bf16_supported = None
        elif hasattr(torch, "xpu") and torch.xpu.is_available():
            device_type = "xpu"
            vendor = "intel"
            try:
                bf16_supported = bool(torch.xpu.is_bf16_supported())
            except Exception:
                bf16_supported = None
    except Exception:
        pass
    return {
        "device_type": device_type,
        "cuda_major": cuda_major,
        "bf16_supported": bf16_supported,
        "vendor": vendor,
        "attention": select_attention(device_type, cuda_major),
        "dtype": select_dtype(device_type, cuda_major, bf16_supported),
    }
