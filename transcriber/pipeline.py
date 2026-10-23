# -*- coding: utf-8 -*-
"""《异键演奏》核心流水线（CLI 与图形界面共用）。

三步流水线：
  1. SheetSage2 音频转录
     输入：原始音频（mp3 / wav / flac / m4a ...）
     输出：melody_vocal.mid、melody_instrumental.mid、chords.mid
           （以及 SheetSage2 原生产物 transcription.mid / events.json）
  2. 物理秒对齐 + 主旋律合并 + 和弦合并
     输入：步骤 1 的三个 MIDI，或一个包含多轨的综合 MIDI
     输出：main_melody.mid / main_melody.json、chords_merged.mid / chords.json
  3. 超音域修正 + 最终导出
     输入：main_melody.mid（+ chords_merged.mid）
     输出：<音频同名>.mid（Main Melody + Chords 两轨）

设计约束：
  * 所有时间一律使用绝对秒；输出 MIDI 统一 480 tpb / 120 BPM 占位；
  * 每一步都是独立函数，可单独调用，也可从中间文件续跑；
  * 主程序只依赖 pretty_midi / mido / numpy，不 import torch。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

# 允许 `python pipeline.py` 与 GUI 以模块方式导入
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# 包导入走相对导入，脚本运行退回扁平导入（保证同一进程只有一份 convert）
try:
    from . import convert as _convert  # noqa: E402
except ImportError:  # 直接运行本文件
    import convert as _convert  # noqa: E402,F401

sys.modules.setdefault("convert", _convert)

from convert import (  # noqa: E402
    DEFAULT_BPM,
    DEFAULT_TPB,
    ChordSlice,
    Note,
    TrackNotes,
    assert_monophonic,
    chord_slices_to_notes,
    chord_slices_to_track,
    count_chord_slice_pitches,
    flatten_notes,
    load_chords_json,
    read_midi_tracks,
    read_notes_any,
    save_chords_json,
    save_tracks_json,
    select_track,
    select_tracks_by_keyword,
    write_midi,
)

# ---------------------------------------------------------------------------
# 路径配置：全部集中在 config.json
#   优先级：环境变量 YIHUAN_*  >  config.json  >  本项目 DEFAULT_CONFIG
# ---------------------------------------------------------------------------
# 本模块位于 <项目根>/transcriber/，配置、vendor、out 等一律相对项目根解析
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_NAME = "config.json"
LOCAL_CONFIG_NAME = "config.local.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "sheetsage2": {
        "home": "vendor/sheetsage2",
        # 首选项目内 venv；不存在时按 python_fallbacks 依次回退
        "python": "vendor/sheetsage2/.venv/Scripts/python.exe",
        "python_fallbacks": [],
        "cli": "vendor/sheetsage2/transcribe_cli.py",
    },
    "ffmpeg": {
        # 空字符串 = 自动（先用 PATH 里的 ffmpeg/ffplay）
        "dir": "",
        "ffmpeg": "",
        "ffplay": "",
    },
    "runtime": {
        "device": "auto",
        "dtype": "bf16",
        "timeout_sec": 3600,
    },
}

_ENV_OVERRIDES = {
    "YIHUAN_SHEETSAGE2_HOME": ("sheetsage2", "home"),
    "YIHUAN_SHEETSAGE2_PY": ("sheetsage2", "python"),
    "YIHUAN_SHEETSAGE2_CLI": ("sheetsage2", "cli"),
    "YIHUAN_FFMPEG_DIR": ("ffmpeg", "dir"),
    "YIHUAN_FFMPEG": ("ffmpeg", "ffmpeg"),
    "YIHUAN_FFPLAY": ("ffmpeg", "ffplay"),
    "YIHUAN_DEVICE": ("runtime", "device"),
    "YIHUAN_DTYPE": ("runtime", "dtype"),
}

EPS = 1e-6
ONSET_TOLERANCE = 0.030          # 和弦切片：起音相差 <=30ms 视为同一片


def config_file() -> Path:
    """config.json 的位置；`YIHUAN_CONFIG` 可以把整份配置指到别处。"""
    raw = (os.environ.get("YIHUAN_CONFIG") or "").strip()
    return Path(raw).expanduser() if raw else (PROJECT_DIR / CONFIG_NAME)


def local_config_file() -> Path:
    """本机私有配置 config.local.json（可选，不入库）。"""
    return PROJECT_DIR / LOCAL_CONFIG_NAME


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """dict 递归合并：override 里没写的键保留 base 的默认值。"""
    out: Dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_nested(data: Dict[str, Any], keys: Sequence[str], value: Any) -> None:
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def load_config() -> Dict[str, Any]:
    """读 config.json + config.local.json（缺文件/坏 JSON 都不抛异常）并叠加环境变量。"""
    cfg = _deep_merge(DEFAULT_CONFIG, {})
    for path in (config_file(), local_config_file()):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, Mapping):
                cfg = _deep_merge(cfg, data)
        except Exception as exc:  # noqa: BLE001
            print(f"[配置] {path} 读取失败，忽略这份配置：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
    for env_name, keys in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw not in (None, ""):
            _set_nested(cfg, keys, raw)
    return cfg


def _as_path(value: Any) -> Optional[Path]:
    """相对路径按项目目录解析；空值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def _pick_path(candidates: Sequence[Any]) -> Tuple[Optional[Path], int]:
    """按顺序取第一个存在的路径；都不存在时返回第一个候选（便于报错）。"""
    paths = [_as_path(v) for v in candidates if str(v or "").strip()]
    paths = [p for p in paths if p is not None]
    if not paths:
        return None, -1
    for index, path in enumerate(paths):
        if path.exists():
            return path, index
    return paths[0], -1


def _source_label(index: int) -> str:
    if index == 0:
        return "config"
    if index > 0:
        return f"fallback[{index}]"
    return "missing"


def resolve_paths(cfg: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """把配置解析成一组绝对路径（只检查是否存在，不检查能否跑起来）。"""
    cfg = load_config() if cfg is None else cfg
    s2 = cfg.get("sheetsage2") or {}
    ff = cfg.get("ffmpeg") or {}
    rt = cfg.get("runtime") or {}

    s2_home = _as_path(s2.get("home")) or (PROJECT_DIR / "vendor" / "sheetsage2")
    s2_py, s2_index = _pick_path(
        [s2.get("python", "")] + list(s2.get("python_fallbacks") or []))
    if s2_py is None:
        s2_py = s2_home / ".venv" / "Scripts" / "python.exe"
    s2_cli = _as_path(s2.get("cli")) or (s2_home / "transcribe_cli.py")

    ff_exe = _as_path(ff.get("ffmpeg"))
    ffplay_exe = _as_path(ff.get("ffplay"))
    ff_dir = _as_path(ff.get("dir"))
    if ff_exe is None and ff_dir is not None:
        candidate = ff_dir / "ffmpeg.exe"
        if candidate.exists():
            ff_exe = candidate
    if ff_exe is None:
        found = shutil.which("ffmpeg")
        if found:
            ff_exe = Path(found)
    if ffplay_exe is None:
        sibling = (ff_exe.parent / "ffplay.exe") if ff_exe is not None else None
        if sibling is not None and sibling.exists():
            ffplay_exe = sibling
        else:
            found = shutil.which("ffplay")
            if found:
                ffplay_exe = Path(found)
    if ff_dir is None:
        ff_dir = ff_exe.parent if ff_exe is not None else None

    try:
        timeout = float(rt.get("timeout_sec") or 3600)
    except (TypeError, ValueError):
        timeout = 3600.0
    if timeout <= 0:
        timeout = 3600.0

    return {
        "config_file": config_file(),
        "project_dir": PROJECT_DIR,
        "sheetsage2": {
            "home": s2_home,
            "python": s2_py,
            "python_source": _source_label(s2_index),
            "cli": s2_cli,
            "cli_exists": s2_cli.is_file(),
        },
        "ffmpeg": {
            "dir": ff_dir,
            "ffmpeg": ff_exe,
            "ffplay": ffplay_exe,
        },
        "runtime": {
            "device": str(rt.get("device") or "auto"),
            "dtype": str(rt.get("dtype") or "bf16"),
            "timeout_sec": timeout,
        },
    }


PATHS = resolve_paths()

SHEETSAGE2_HOME: Path = PATHS["sheetsage2"]["home"]
SHEETSAGE2_PY: Path = PATHS["sheetsage2"]["python"]
SHEETSAGE2_CLI: Path = PATHS["sheetsage2"]["cli"]
MERT_DIR: Path = SHEETSAGE2_HOME / "models" / "MERT-v2-FullSong"


class AIComponentsMissing(RuntimeError):
    """AI 扒谱组件（推理环境或模型权重）尚未安装。"""


def ai_components_ready() -> bool:
    """推理环境 + 两个模型权重是否齐全；缺任何一项都视为未安装。"""
    if not SHEETSAGE2_PY.exists() or not SHEETSAGE2_CLI.exists():
        return False
    for name in ("SheetSage2", "MERT-v2-FullSong"):
        if not (SHEETSAGE2_HOME / "models" / name / "model.safetensors").exists():
            return False
    return True
SHEETSAGE2_MODEL_DIR: Path = SHEETSAGE2_HOME / "models" / "SheetSage2"
FFMPEG_DIR: Optional[Path] = PATHS["ffmpeg"]["dir"]
FFMPEG_EXE: Optional[Path] = PATHS["ffmpeg"]["ffmpeg"]
FFPLAY_EXE: Optional[Path] = PATHS["ffmpeg"]["ffplay"]

STEP_TIMEOUT = PATHS["runtime"]["timeout_sec"]
DEFAULT_DEVICE = PATHS["runtime"]["device"]
DEFAULT_DTYPE = PATHS["runtime"]["dtype"]


def describe_paths(paths: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """把生效的路径摊平成可 JSON 序列化的 dict（写日志 / 交给 GUI 显示）。"""
    p = PATHS if paths is None else paths

    def _s(value: Any) -> str:
        return str(value) if value else ""

    ffmpeg_info = p["ffmpeg"]
    return {
        "config_file": _s(p["config_file"]),
        "project_dir": _s(p["project_dir"]),
        "sheetsage2": {
            "home": _s(p["sheetsage2"]["home"]),
            "python": _s(p["sheetsage2"]["python"]),
            "python_source": p["sheetsage2"]["python_source"],
            "python_exists": Path(p["sheetsage2"]["python"]).is_file(),
            "cli": _s(p["sheetsage2"]["cli"]),
            "cli_exists": bool(p["sheetsage2"]["cli_exists"]),
            "model_exists": (SHEETSAGE2_MODEL_DIR / "model.safetensors").is_file(),
            "mert_exists": (MERT_DIR / "model.safetensors").is_file(),
        },
        "ffmpeg": {
            "dir": _s(ffmpeg_info["dir"]),
            "ffmpeg": _s(ffmpeg_info["ffmpeg"]),
            "ffplay": _s(ffmpeg_info["ffplay"]),
        },
        "runtime": dict(p["runtime"]),
    }


def print_paths(stream=None) -> Dict[str, Any]:
    """在日志里打出生效路径（便于确认用的是项目内模型 / vendor 代码）。"""
    info = describe_paths()
    out = stream or sys.stdout
    print("[路径] 配置文件：" + info["config_file"], file=out)
    print("[路径] 项目目录：" + info["project_dir"], file=out)
    s2 = info["sheetsage2"]
    print(f"[路径] SheetSage2 CLI：{s2['cli']}（存在={s2['cli_exists']}）", file=out)
    print(f"[路径] SheetSage2 Python：{s2['python']}"
          f"（来源={s2['python_source']}，存在={s2['python_exists']}）", file=out)
    print(f"[路径] SheetSage2 权重：{SHEETSAGE2_MODEL_DIR}（存在={s2['model_exists']}）",
          file=out)
    print(f"[路径] MERT 权重：{MERT_DIR}（存在={s2['mert_exists']}）", file=out)
    print(f"[路径] ffmpeg：{info['ffmpeg']['ffmpeg'] or '未找到'}", file=out)
    print(f"[路径] ffplay：{info['ffmpeg']['ffplay'] or '未找到'}", file=out)
    return info


# ---------------------------------------------------------------------------
# 设备探测：用 SheetSage2 的 Python（带 torch / XPU）探测，主程序不 import torch
# ---------------------------------------------------------------------------
_PROBE_CODE = r"""
import json
info = {"xpu": False, "cuda": False, "name": "", "torch": "",
        "bf16": False, "fp16": False, "cuda_build": ""}
try:
    import torch
    info["torch"] = str(torch.__version__)
    try:
        info["cuda_build"] = str(getattr(torch.version, "cuda", "") or "")
    except Exception:
        info["cuda_build"] = ""
    if hasattr(torch, "xpu"):
        try:
            info["xpu"] = bool(torch.xpu.is_available())
            if info["xpu"]:
                info["fp16"] = True
                try:
                    info["bf16"] = bool(torch.xpu.is_bf16_supported())
                except Exception:
                    info["bf16"] = True
                try:
                    info["name"] = str(torch.xpu.get_device_name(0))
                except Exception:
                    pass
        except Exception:
            pass
    try:
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["fp16"] = True
            try:
                info["bf16"] = bool(torch.cuda.is_bf16_supported())
            except Exception:
                info["bf16"] = False
            if not info["name"]:
                try:
                    info["name"] = str(torch.cuda.get_device_name(0))
                except Exception:
                    pass
    except Exception:
        pass
except Exception as exc:
    info["error"] = str(exc)
print(json.dumps(info))
"""

_DEVICE_CACHE: Optional[Dict[str, Any]] = None


def probe_device(force: bool = False) -> Dict[str, Any]:
    """返回 {'best': 'xpu'|'cuda'|'cpu', 'xpu':..., 'cuda':..., 'name':...}。"""
    global _DEVICE_CACHE
    if _DEVICE_CACHE is not None and not force:
        return dict(_DEVICE_CACHE)

    info: Dict[str, Any] = {"xpu": False, "cuda": False, "name": "",
                            "torch": "", "bf16": False, "fp16": False,
                            "cuda_build": "", "ai_ready": ai_components_ready(),
                            "probe_python": str(SHEETSAGE2_PY), "error": ""}
    if not SHEETSAGE2_PY.exists():
        info["error"] = f"探测 Python 不存在: {SHEETSAGE2_PY}"
    else:
        try:
            proc = subprocess.run(
                [str(SHEETSAGE2_PY), "-c", _PROBE_CODE],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180,
                **no_window_kwargs(),
            )
            payload = None
            for line in reversed((proc.stdout or "").strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    payload = json.loads(line)
                    break
            if payload is None:
                info["error"] = (proc.stderr or "设备探测无输出")[-800:]
            else:
                info.update({k: payload.get(k, info[k]) for k in
                             ("xpu", "cuda", "name", "torch", "bf16",
                              "fp16", "cuda_build", "error")})
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"

    if info["xpu"]:
        info["best"] = "xpu"
    elif info["cuda"]:
        info["best"] = "cuda"
    else:
        info["best"] = "cpu"
    _DEVICE_CACHE = dict(info)
    return dict(info)


def resolve_device(requested: str = "auto") -> str:
    """把 auto / xpu / cuda / cpu 解析成实际可用设备；不可用则回退。"""
    req = (requested or "auto").strip().lower()
    info = probe_device()
    available = {"cpu"}
    if info.get("xpu"):
        available.add("xpu")
    if info.get("cuda"):
        available.add("cuda")
    if req == "auto":
        return info.get("best") or "cpu"
    if req in available:
        return req
    print(f"[提示] 请求的设备 {req} 不可用，自动改用 {info.get('best') or 'cpu'}", flush=True)
    return info.get("best") or "cpu"


def resolve_dtype(device: str, requested: str = "bf16") -> str:
    """跨显卡 / CPU 精度自适应，防止 bf16 在旧 N 卡或 CPU 上崩。

    * cuda 且不支持 bf16 → fp16
    * cpu → fp32
    * xpu / 支持 bf16 的 cuda → bf16
    """
    req = (requested or "bf16").strip().lower()
    info = probe_device()
    dev = (device or "cpu").lower()
    if req == "bf16":
        if dev in ("cuda", "xpu") and not info.get("bf16"):
            print("[提示] 当前 %s 显卡不支持 bf16，自动降级为 fp16" % dev.upper(), flush=True)
            return "fp16"
        if dev == "cpu":
            print("[提示] CPU 下 bf16 算子不可用/过慢，自动降级为 fp32", flush=True)
            return "fp32"
        return "bf16"
    if req == "fp16":
        if dev == "cpu":
            print("[提示] CPU 下 fp16 不可用，自动降级为 fp32", flush=True)
            return "fp32"
        return "fp16"
    return "fp32"


def device_status() -> Dict[str, Any]:
    info = probe_device()
    info["requested"] = DEFAULT_DEVICE
    info["active"] = resolve_device(DEFAULT_DEVICE)
    info["dtype"] = resolve_dtype(info["active"], DEFAULT_DTYPE)
    return info


def device_label(active: Optional[str] = None) -> str:
    info = probe_device()
    key = (active or info.get("best") or "cpu").lower()
    name = info.get("name") or ""
    if key == "xpu":
        return f"XPU（{name or 'Intel Arc'}）"
    if key == "cuda":
        return f"CUDA（{name or 'NVIDIA'}）"
    return "CPU"


# ---------------------------------------------------------------------------
# 子进程执行（实时回显 + 落日志 + 可取消）
# ---------------------------------------------------------------------------
_CURRENT_PROC: Optional[subprocess.Popen] = None
_CANCEL = threading.Event()
_PROC_LOCK = threading.Lock()


def reset_cancel() -> None:
    """每次开始新任务前清掉取消标记。"""
    _CANCEL.clear()


def cancel_current() -> bool:
    """取消当前正在跑的子进程；返回是否真的杀掉了进程。"""
    _CANCEL.set()
    with _PROC_LOCK:
        proc = _CURRENT_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _make_env() -> Dict[str, str]:
    env = os.environ.copy()
    if FFMPEG_DIR and Path(FFMPEG_DIR).exists():
        env["PATH"] = str(FFMPEG_DIR) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _tail(text: str, lines: int = 8) -> str:
    rows = [r for r in (text or "").strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def no_window_kwargs(hide: bool = True) -> Dict[str, Any]:
    """subprocess 启动参数：不弹控制台窗口（Windows 下生效）。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0 if hide else 1
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}


def run_command(
    cmd: Sequence[str],
    log_path: Union[str, Path],
    *,
    timeout: float = STEP_TIMEOUT,
    cwd: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """执行外部命令，实时回显并落日志，失败时抛出带日志尾部的异常。"""
    global _CURRENT_PROC

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(c) for c in cmd]
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_make_env(),
            cwd=str(cwd) if cwd else None,
            **no_window_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"命令启动失败（{type(exc).__name__}: {exc}）：{' '.join(cmd)}") from exc

    with _PROC_LOCK:
        _CURRENT_PROC = proc

    chunks: List[str] = []

    def pump() -> None:
        """边读边回显：GUI 靠这些行解析进度。"""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                chunks.append(line)
                try:
                    print(line.rstrip("\r\n"), flush=True)
                except Exception:  # noqa: BLE001  没有可用 stdout 时只落日志
                    pass
        except Exception:  # noqa: BLE001  输出流断开不影响退出码判断
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    timed_out = False
    cancelled = False
    code = -1
    try:
        while True:
            if _CANCEL.is_set():
                cancelled = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                code = proc.wait()
                break
            try:
                code = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if time.time() - started > timeout:
                    timed_out = True
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    code = proc.wait()
                    break
    finally:
        with _PROC_LOCK:
            _CURRENT_PROC = None

    reader.join(timeout=5)
    elapsed = time.time() - started
    output = "".join(chunks)

    header = (
        f"# command: {' '.join(cmd)}\n"
        f"# cwd: {cwd or PROJECT_DIR}\n"
        f"# exit: {code}\n"
        f"# elapsed_sec: {elapsed:.3f}\n"
        f"# cancelled: {bool(cancelled)}\n"
        f"\n--- OUTPUT (stdout + stderr) ---\n"
    )
    log_path.write_text(header + output, encoding="utf-8", errors="replace")

    if cancelled or _CANCEL.is_set():
        raise RuntimeError("任务已被用户取消。")
    if timed_out:
        raise RuntimeError(f"命令超时（{timeout:.0f}s，已跑 {elapsed:.1f}s）：{' '.join(cmd)}\n"
                           f"日志: {log_path}")
    if code != 0:
        raise RuntimeError(
            f"命令退出码 {code}：{' '.join(cmd)}\n"
            f"日志: {log_path}\n"
            f"输出尾部:\n{_tail(output, 12)}"
        )
    print(f"    · 子进程完成 {elapsed:.1f}s，日志 {log_path.name}")
    return {
        "elapsed_sec": elapsed,
        "stdout": output,
        "stderr": "",
        "log": str(log_path),
        "command": cmd,
    }


# ---------------------------------------------------------------------------
# 通用输入定位
# ---------------------------------------------------------------------------
def _first_existing(directory: Path, names: Iterable[str]) -> Optional[Path]:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _read_tracks(path: Union[str, Path]) -> List[TrackNotes]:
    return read_notes_any(path)


def _chord_notes_from(path: Union[str, Path], *, fallback_all: bool) -> List[Note]:
    """从 MIDI / JSON 里取所有“和弦轨”的音符（轨名含 chord）。"""
    path = Path(path)
    if path.suffix.lower() == ".json":
        return chord_slices_to_notes(load_chords_json(path))
    tracks = read_midi_tracks(path)
    hits = select_tracks_by_keyword(tracks, "chord", fallback_all=fallback_all)
    return flatten_notes(hits)


def _dedupe_notes(notes: Iterable[Note]) -> List[Note]:
    """同一批和弦常同时出现在 transcription.mid 与 chords.mid，按音高+秒级起止去重。"""
    seen = set()
    out: List[Note] = []
    for note in notes:
        key = (note.pitch, round(note.start_sec, 6), round(note.end_sec, 6), note.velocity)
        if key in seen:
            continue
        seen.add(key)
        out.append(note)
    out.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    return out


def _validate_env() -> None:
    """步骤 1 之前的硬性检查，错误信息写成用户能看懂的话。"""
    if not SHEETSAGE2_PY.is_file():
        raise FileNotFoundError(
            f"找不到 SheetSage2 的 Python：{SHEETSAGE2_PY}\n"
            f"请把可用的 XPU 虚拟环境放到该位置（或在本项目 config.json 里改 "
            f"sheetsage2.python）。")
    if not SHEETSAGE2_CLI.is_file():
        raise FileNotFoundError(
            f"找不到 SheetSage2 转录脚本：{SHEETSAGE2_CLI}\n"
            f"请确认 vendor\\sheetsage2\\ 目录完整。")
    for name, path in (("SheetSage2", SHEETSAGE2_MODEL_DIR / "model.safetensors"),
                       ("MERT-v2-FullSong", MERT_DIR / "model.safetensors")):
        if not path.is_file():
            raise FileNotFoundError(
                f"缺少 {name} 模型权重：{path}\n"
                f"请把 model.safetensors 放回该目录（约 2.4GB / 0.2GB）。")


# ---------------------------------------------------------------------------
# 步骤 1：SheetSage2 音频转录
# ---------------------------------------------------------------------------
def step1(
    audio: Union[str, Path],
    out_dir: Union[str, Path],
    *,
    device: str = DEFAULT_DEVICE,
    dtype: str = DEFAULT_DTYPE,
    timeout: float = STEP_TIMEOUT,
    max_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """原始音频 -> melody_vocal.mid / melody_instrumental.mid / chords.mid。"""
    t0 = time.time()
    audio = Path(audio)
    if not audio.is_file():
        raise FileNotFoundError(f"音频不存在：{audio}")
    if not ai_components_ready():
        raise AIComponentsMissing("尚未安装 AI 音频扒谱模型组件（约 2.6 GB）")
    _validate_env()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "sheetsage2"
    raw.mkdir(parents=True, exist_ok=True)

    dev = resolve_device(device)
    dtype = resolve_dtype(dev, dtype)
    cmd = [str(SHEETSAGE2_PY), str(SHEETSAGE2_CLI), str(audio),
           "--output", str(raw), "--device", dev, "--dtype", dtype]
    if max_seconds:
        cmd += ["--max-seconds", str(float(max_seconds))]

    print(f"[步骤1/3] SheetSage2 音频转录（设备：{device_label(dev)}，精度：{dtype}）")
    print(f"    CLI：{SHEETSAGE2_CLI}")
    if dev == "cpu":
        print("    CPU 推理：一首歌约 2~4 分钟（模型固定按 5 分钟窗计算），"
              "进度长时间停在 0% 是正常的，请勿关闭窗口")
    run = run_command(cmd, out / "logs" / "step1_sheetsage2.log", timeout=timeout)

    # 把三个分轨复制到输出根目录，便于单独导入 / 导出
    copied: Dict[str, str] = {}
    for name in ("melody_vocal.mid", "melody_instrumental.mid", "chords.mid",
                 "transcription.mid"):
        src = raw / name
        if src.is_file():
            dst = out / name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            copied[name] = str(dst)

    if "melody_vocal.mid" not in copied and "transcription.mid" not in copied:
        raise FileNotFoundError(
            f"SheetSage2 没有产出人声旋律（melody_vocal.mid / transcription.mid）：{raw}\n"
            f"请查看日志：{out / 'logs' / 'step1_sheetsage2.log'}")

    # 统计（只读，用于界面显示）
    vocal_notes: List[Note] = []
    if (raw / "melody_vocal.mid").is_file():
        vocal_notes = select_track(read_midi_tracks(raw / "melody_vocal.mid"),
                                   ["Vocal"], fallback_all=True).notes
    ins_notes: List[Note] = []
    if (raw / "melody_instrumental.mid").is_file():
        ins_notes = select_track(read_midi_tracks(raw / "melody_instrumental.mid"),
                                 ["Ins", "Instrumental"], fallback_all=True).notes
    chord_notes: List[Note] = []
    for src, fallback in ((raw / "transcription.mid", False), (raw / "chords.mid", True)):
        if src.is_file():
            chord_notes.extend(_chord_notes_from(src, fallback_all=fallback))
    chord_notes = _dedupe_notes(chord_notes)
    slices = _notes_to_slices(chord_notes)

    result = {
        "step": 1,
        "name": "SheetSage2",
        "elapsed_sec": time.time() - t0,
        "run_elapsed_sec": run["elapsed_sec"],
        "device": dev,
        "device_label": device_label(dev),
        "dtype": dtype,
        "outputs": {
            **copied,
            "raw_dir": str(raw),
            "log": run["log"],
        },
        "counts": {
            "melody_vocal": len(vocal_notes),
            "melody_instrumental": len(ins_notes),
            "chords": len(chord_notes),
            "chord_slices": len(slices),
        },
    }
    print(f"[步骤1/3] 完成 {result['elapsed_sec']:.1f}s：Vocal {len(vocal_notes)} 音，"
          f"Ins {len(ins_notes)} 音，Chords {len(chord_notes)} 音 -> {len(slices)} 片")
    return result


def _notes_to_slices(notes: Iterable[Note]) -> List[ChordSlice]:
    return _convert.notes_to_chord_slices(notes, onset_tolerance=ONSET_TOLERANCE)


# ---------------------------------------------------------------------------
# 步骤 2：物理秒对齐 + 主旋律合并 + 和弦合并
# ---------------------------------------------------------------------------
def _better_note(candidate: Note, current: Note, mode: str) -> bool:
    """重叠时谁胜出：duration（默认）或 velocity。"""
    if mode == "velocity":
        if candidate.velocity != current.velocity:
            return candidate.velocity > current.velocity
        return candidate.duration > current.duration
    if mode not in ("duration", "velocity"):
        raise ValueError(f"merge_mode 只能是 duration / velocity，收到：{mode}")
    if abs(candidate.duration - current.duration) > 1e-9:
        return candidate.duration > current.duration
    return candidate.velocity > current.velocity


def merge_melodies(
    vocal_notes: Sequence[Note],
    instrumental_notes: Sequence[Note],
    *,
    merge_mode: str = "duration",
) -> List[Note]:
    """把 Vocal 与 Ins 合成严格单音；重叠时按时长 / 力度二选一。"""
    incoming: List[Note] = [n.copy() for n in list(vocal_notes) + list(instrumental_notes)]
    incoming = [n for n in incoming if n.end_sec > n.start_sec + 1e-9]
    incoming.sort(key=lambda n: (n.start_sec, -n.duration, -n.velocity, n.pitch))

    selected: List[Note] = []
    for note in incoming:
        if not selected:
            selected.append(note)
            continue
        last = selected[-1]
        if note.start_sec < last.end_sec - EPS:
            if _better_note(note, last, merge_mode):
                selected[-1] = note
        else:
            selected.append(note)

    selected.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    assert_monophonic(selected)
    return selected


def _detect_tempo_from_beats(out_dir: Union[str, Path]) -> float:
    """从 SheetSage2 的 beat.lab / song_beats.txt 读真实 BPM；没有就回退 120。"""
    out = Path(out_dir)
    candidates = (
        out / "sheetsage2" / "beat.lab",
        out / "sheetsage2" / "notation" / "song_beats.txt",
        out / "beat.lab",
    )
    for path in candidates:
        if not path.is_file():
            continue
        stamps = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if parts:
                    stamps.append(float(parts[0]))
        except Exception:
            continue
        stamps.sort()
        ibis = [b - a for a, b in zip(stamps, stamps[1:]) if 0.30 <= (b - a) <= 1.00]
        if ibis:
            med = statistics.median(ibis)
            if 0.30 <= med <= 1.00:
                return round(60.0 / med, 2)
    return float(DEFAULT_BPM)


def step2(
    out_dir: Union[str, Path],
    *,
    vocal: Optional[Union[str, Path]] = None,
    instrumental: Optional[Union[str, Path]] = None,
    chords: Optional[Union[str, Path]] = None,
    integrated: Optional[Union[str, Path]] = None,
    merge_mode: str = "duration",
) -> Dict[str, Any]:
    """物理秒对齐 + 主旋律单音合并 + 和弦合并。

    输入二选一（也可以同时给）：
      * 三个分轨：vocal / instrumental / chords；
      * 一个综合 MIDI：integrated（如 transcription.mid）。
    未显式给出的输入，会自动在 out_dir 里找同名文件。
    """
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    vocal_path = Path(vocal) if vocal else _first_existing(
        out, ("melody_vocal.mid", "melody_vocal.json"))
    ins_path = Path(instrumental) if instrumental else _first_existing(
        out, ("melody_instrumental.mid", "melody_instrumental.json"))
    chords_path = Path(chords) if chords else _first_existing(out, ("chords.mid", "chords.json"))
    integrated_path = Path(integrated) if integrated else _first_existing(
        out, ("transcription.mid",))

    if not vocal_path and not ins_path and not integrated_path:
        raise FileNotFoundError(
            f"没有可用的输入：请给出 vocal / instrumental / chords，或一个综合 MIDI。\n"
            f"在 {out} 下也没找到 melody_vocal.mid / transcription.mid。")

    vocal_notes: List[Note] = []
    if vocal_path and vocal_path.is_file():
        vocal_notes = select_track(_read_tracks(vocal_path), ["Vocal"], fallback_all=True).notes
    ins_notes: List[Note] = []
    if ins_path and ins_path.is_file():
        ins_notes = select_track(_read_tracks(ins_path),
                                 ["Ins", "Instrumental"], fallback_all=True).notes

    integrated_tracks: List[TrackNotes] = []
    if integrated_path and integrated_path.is_file():
        integrated_tracks = _read_tracks(integrated_path)
        if not vocal_notes:
            vocal_notes = select_track(integrated_tracks, ["Vocal"],
                                       fallback_all=False).notes
        if not ins_notes:
            ins_notes = select_track(integrated_tracks, ["Ins", "Instrumental"],
                                     fallback_all=False).notes

    # 和弦：分开的 chords 文件 + 综合 MIDI 里所有轨名含 chord 的轨，全部合并后去重
    chord_notes: List[Note] = []
    if chords_path and chords_path.is_file():
        chord_notes.extend(_chord_notes_from(chords_path, fallback_all=True))
    if integrated_tracks:
        chord_notes.extend(flatten_notes(select_tracks_by_keyword(
            integrated_tracks, "chord", fallback_all=False)))
    chord_notes = _dedupe_notes(chord_notes)
    slices = _notes_to_slices(chord_notes)

    # 主旋律单音化
    merged = merge_melodies(vocal_notes, ins_notes, merge_mode=merge_mode)
    melody_track = TrackNotes("Main Melody", merged)
    chord_track = chord_slices_to_track(slices, "Chords")

    tempo = _detect_tempo_from_beats(out)
    save_tracks_json(out / "main_melody.json", [melody_track], kind="main_melody",
                     source=Path(vocal_path).name if vocal_path else "",
                     tempo_bpm=tempo,
                     extra={"merge_mode": merge_mode, "merge_priority": merge_mode})
    write_midi(out / "main_melody.mid", [melody_track], bpm=tempo)
    save_chords_json(out / "chords.json", slices)
    write_midi(out / "chords_merged.mid", [chord_track], bpm=tempo)

    span = [round(min((n.start_sec for n in merged), default=0.0), 6),
            round(max((n.end_sec for n in merged), default=0.0), 6)]

    result = {
        "step": 2,
        "name": "align_merge",
        "elapsed_sec": time.time() - t0,
        "merge_mode": merge_mode,
        "inputs": {
            "vocal": str(vocal_path) if vocal_path else "",
            "instrumental": str(ins_path) if ins_path else "",
            "chords": str(chords_path) if chords_path else "",
            "integrated": str(integrated_path) if integrated_path else "",
        },
        "outputs": {
            "main_melody.mid": str(out / "main_melody.mid"),
            "main_melody.json": str(out / "main_melody.json"),
            "chords_merged.mid": str(out / "chords_merged.mid"),
            "chords.json": str(out / "chords.json"),
        },
        "counts": {
            "vocal": len(vocal_notes),
            "instrumental": len(ins_notes),
            "merged": len(merged),
            "chords": len(chord_notes),
            "chord_slices": len(slices),
            "chord_pitches": count_chord_slice_pitches(slices),
        },
        "span_sec": span,
    }
    print(f"[步骤2/3] 完成 {result['elapsed_sec']:.1f}s：Vocal {len(vocal_notes)} 音 + "
          f"Ins {len(ins_notes)} 音 -> 单音主旋律 {len(merged)} 音；"
          f"和弦 {len(chord_notes)} 音 -> {len(slices)} 片 / {chord_track.name}")
    return result


# ---------------------------------------------------------------------------
# 步骤 3：超音域修正 + 最终 MIDI 导出
# ---------------------------------------------------------------------------
def fix_pitch_range(
    notes: Sequence[Note],
    *,
    low: int = 40,
    high: int = 88,
    instrument_mode: str = "acoustic",
    isolated_duration: float = 0.35,
    isolated_semitones: int = 10,
) -> Tuple[List[Note], Dict[str, int]]:
    """按固定音域修正，返回 (修正后音符, 修正统计)。

    严格按 3 步顺序：
      1) 超界孤立倍频噪点：pitch > high、比前后紧邻都高 >=10、时值 <0.35s → -12；
      2) 保全音程的整轨平移：
         2A 物理超界无损平移（±12，绝对优先于单音折断）；
         2B 木吉他模式专属：>76 高音衰减区占比 >25% 时再整体 -12；
      3) 单音边界兜底（while >high -> -12 / while <low -> +12），
         记入 `boundary_folded`。

    `instrument_mode`：`"acoustic"`（默认，木吉他会做 2B）/ `"electric"`（跳过 2B）。
    """
    if low < 0 or high > 127 or low >= high:
        raise ValueError(f"音域参数非法：{low}-{high}")
    if instrument_mode not in ("acoustic", "electric"):
        raise ValueError(f"instrument_mode 只能是 acoustic / electric：{instrument_mode}")
    fixed = [n.copy() for n in notes]
    stats: Dict[str, int] = {
        "global_down_octaves": 0, "global_up_octaves": 0,
        "isolated_fixed": 0, "low_fixed": 0, "high_fixed": 0,
        "boundary_folded": 0, "acoustic_extra_shift": 0,
        "instrument_mode": instrument_mode,
    }
    if not fixed:
        return fixed, stats

    # ---- 第 1 步：先除噪（防止单颗噪点拉偏整轨判断）----
    current = [n.pitch for n in fixed]
    for i in range(1, len(fixed) - 1):
        p = current[i]
        if (p > high
                and p - current[i - 1] >= isolated_semitones
                and p - current[i + 1] >= isolated_semitones
                and fixed[i].duration < isolated_duration):
            fixed[i].pitch -= 12
            stats["isolated_fixed"] += 1

    def _stats_now():
        ps = sorted(n.pitch for n in fixed)
        med = statistics.median(ps)
        over = sum(1 for p in ps if p > high)
        return ps, med, over / float(len(ps))

    # ---- 第 2A 步：整轨八度平移（最多 3 次）----
    while stats["global_down_octaves"] + stats["global_up_octaves"] < 3:
        ps, med, over_ratio = _stats_now()
        top, bottom = ps[-1], ps[0]
        if top > high and (bottom - 12 >= low
                           or med >= high - 6
                           or (over_ratio >= 0.40 and med >= 72)):
            for n in fixed:
                n.pitch -= 12
            stats["global_down_octaves"] += 1
            continue
        if bottom < low and top + 12 <= high:
            for n in fixed:
                n.pitch += 12
            stats["global_up_octaves"] += 1
            continue
        break

    # ---- 第 2B 步：木吉他高音衰减区（只在 2A 没动过整轨时做一次）----
    if instrument_mode == "acoustic" and stats["global_down_octaves"] == 0:
        ps, _med, _ratio = _stats_now()
        high_ratio = sum(1 for p in ps if p > 76) / float(len(ps))
        if high_ratio > 0.25 and (min(ps) - 12) >= 45:
            for n in fixed:
                n.pitch -= 12
            stats["global_down_octaves"] += 1
            stats["acoustic_extra_shift"] = 1

    # ---- 第 3 步：单音边界兜底 ----
    for n in fixed:
        while n.pitch > high:
            n.pitch -= 12
            stats["high_fixed"] += 1
            stats["boundary_folded"] += 1
        while n.pitch < low:
            n.pitch += 12
            stats["low_fixed"] += 1
            stats["boundary_folded"] += 1

    actual_low = min(n.pitch for n in fixed)
    actual_high = max(n.pitch for n in fixed)
    if actual_low < low or actual_high > high:
        raise AssertionError(f"音域修正失败：{actual_low}~{actual_high}，目标 {low}~{high}")
    return fixed, stats


def correct_pitch_range(notes: Sequence[Note], *, low: int = 40, high: int = 88,
                        instrument_mode: str = "acoustic") -> List[Note]:
    """便于 GUI / 测试调用的简化版：只返回修正后的音符。"""
    return fix_pitch_range(notes, low=low, high=high,
                           instrument_mode=instrument_mode)[0]


def step3(
    main_melody: Optional[Union[str, Path]],
    out_dir: Union[str, Path],
    *,
    chords: Optional[Union[str, Path]] = None,
    low: int = 40,
    high: int = 88,
    instrument_mode: str = "acoustic",
    final_name: Optional[str] = None,
    source_audio: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """超音域修正 + 导出最终 MIDI（Main Melody + Chords 两轨）。

    `instrument_mode`：`"acoustic"`（默认，木吉他高音衰减区整体下移）/
                       `"electric"`（电吉他，保留高把位）。
    """
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    melody_path = Path(main_melody) if main_melody else _first_existing(
        out, ("main_melody.mid", "main_melody.json"))
    if melody_path is None or not Path(melody_path).is_file():
        raise FileNotFoundError(
            f"找不到主旋律文件：{melody_path}\n请先跑步骤 2，或用参数指定 main_melody.mid。")
    chord_path = Path(chords) if chords else _first_existing(
        out, ("chords_merged.mid", "chords.json"))

    melody_tracks = _read_tracks(melody_path)
    melody_track = select_track(melody_tracks, ["Main Melody", "Melody"], fallback_all=True)
    if not melody_track.notes:
        raise ValueError(f"主旋律文件里没有音符：{melody_path}")

    fixed, stats = fix_pitch_range(melody_track.notes, low=low, high=high,
                                   instrument_mode=instrument_mode)
    fixed.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    assert_monophonic(fixed)

    slices: List[ChordSlice] = []
    if chord_path and Path(chord_path).is_file():
        if Path(chord_path).suffix.lower() == ".json":
            slices = load_chords_json(chord_path)
        else:
            chord_notes = _chord_notes_from(chord_path, fallback_all=True)
            slices = _notes_to_slices(chord_notes)
    chord_track = chord_slices_to_track(slices, "Chords")

    # 最终文件名：显式指定 > 音频同名 > 通用名
    if final_name:
        name = str(final_name).strip()
        if not name.lower().endswith((".mid", ".midi")):
            name += ".mid"
    elif source_audio:
        name = Path(source_audio).stem + ".mid"
    else:
        name = "final.mid"
    final_path = out / name

    tempo = _detect_tempo_from_beats(out)
    if abs(tempo - 120.0) < 1e-9:
        tempo = _detect_tempo_from_beats(Path(melody_path).parent)
    write_midi(final_path, [TrackNotes("Main Melody", fixed), chord_track], bpm=tempo)

    result = {
        "step": 3,
        "name": "export_final",
        "elapsed_sec": time.time() - t0,
        "range": [low, high],
        "outputs": {"final_midi": str(final_path)},
        "counts": {
            "main_melody": len(fixed),
            "chord_slices": len(slices),
            "chord_pitches": count_chord_slice_pitches(slices),
            "tracks": 2,
        },
        "pitch_range": [min((n.pitch for n in fixed), default=None),
                        max((n.pitch for n in fixed), default=None)],
        "instrument_mode": instrument_mode,
        "tempo_bpm": tempo,
        "corrections": stats,
    }
    print(f"[步骤3/3] 完成 {result['elapsed_sec']:.1f}s：主旋律 {len(fixed)} 音，"
          f"音域 {result['pitch_range'][0]}~{result['pitch_range'][1]}"
          f"（{instrument_mode}，整轨 -{stats['global_down_octaves'] * 12} 半音，"
          f"孤立修正 {stats['isolated_fixed']}，单音折断 {stats['boundary_folded']}）；"
          f"和弦 {len(slices)} 片 -> {final_path.name}")
    return result


# ---------------------------------------------------------------------------
# 一键全流程
# ---------------------------------------------------------------------------
def run_all(
    audio: Union[str, Path],
    out_dir: Union[str, Path],
    *,
    device: str = DEFAULT_DEVICE,
    dtype: str = DEFAULT_DTYPE,
    merge_mode: str = "duration",
    low: int = 40,
    high: int = 88,
    instrument_mode: str = "acoustic",
    timeout: float = STEP_TIMEOUT,
    max_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """音频 -> 步骤 1 -> 步骤 2 -> 步骤 3，返回完整结果。"""
    t0 = time.time()
    audio = Path(audio)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print(" 《异键演奏》一键全流程：① 转录 -> ② 合并 -> ③ 导出")
    print(f" 输入：{audio}")
    print(f" 输出：{out}")
    print("=" * 66)

    r1 = step1(audio, out, device=device, dtype=dtype, timeout=timeout,
               max_seconds=max_seconds)
    r2 = step2(out, merge_mode=merge_mode)
    r3 = step3(out / "main_melody.mid", out,
               chords=out / "chords_merged.mid", low=low, high=high,
               instrument_mode=instrument_mode, source_audio=audio)

    total = time.time() - t0
    summary = {
        "audio": str(audio),
        "out_dir": str(out),
        "device": r1["device"],
        "device_label": r1["device_label"],
        "dtype": dtype,
        "total_sec": total,
        "final_midi": r3["outputs"]["final_midi"],
        "steps": [r1, r2, r3],
    }
    (out / "pipeline_timings.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 66)
    print(f" 全部完成，用时 {total:.1f}s")
    print(f" 最终 MIDI：{r3['outputs']['final_midi']}")
    print("=" * 66)
    return summary


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def _add_device_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", "-d", default=DEFAULT_DEVICE,
                        choices=["auto", "xpu", "cuda", "cpu"], help="推理设备")
    parser.add_argument("--dtype", default=DEFAULT_DTYPE,
                        choices=["bf16", "fp16", "fp32"], help="推理精度")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="《异键演奏》三步流水线（SheetSage2 单一模型）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="一键全流程：转录 -> 合并 -> 导出")
    p_run.add_argument("audio", help="原始音频文件")
    p_run.add_argument("--out", "-o", required=True, help="输出目录")
    _add_device_args(p_run)
    p_run.add_argument("--merge-mode", default="duration", choices=["duration", "velocity"],
                       help="主旋律重叠时的保留规则")
    p_run.add_argument("--low", type=int, default=40, help="主旋律最低音（默认 40 / E2）")
    p_run.add_argument("--high", type=int, default=88, help="主旋律最高音（默认 88 / E6）")
    p_run.add_argument("--instrument", dest="instrument_mode", default="acoustic",
                       choices=["acoustic", "electric"],
                       help="吉他音色适配：acoustic 木吉他（默认）/ electric 电吉他")
    p_run.add_argument("--max-seconds", type=float, default=None, help="仅转录前 N 秒（调试用）")

    p1 = sub.add_parser("step1", help="只跑步骤 1：SheetSage2 音频转录")
    p1.add_argument("--in", dest="input", required=True, help="原始音频文件")
    p1.add_argument("--out", "-o", required=True, help="输出目录")
    _add_device_args(p1)
    p1.add_argument("--max-seconds", type=float, default=None, help="仅转录前 N 秒（调试用）")

    p2 = sub.add_parser("step2", help="只跑步骤 2：秒级对齐 + 主旋律 / 和弦合并")
    p2.add_argument("--out", "-o", required=True, help="输出目录（默认也从这里找输入）")
    p2.add_argument("--vocal", default=None, help="人声旋律 MIDI / JSON")
    p2.add_argument("--instrumental", default=None, help="器乐旋律 MIDI / JSON")
    p2.add_argument("--chords", default=None, help="和弦 MIDI / JSON")
    p2.add_argument("--integrated", default=None, help="综合 MIDI（如 transcription.mid）")
    p2.add_argument("--merge-mode", default="duration", choices=["duration", "velocity"],
                    help="主旋律重叠时的保留规则")

    p3 = sub.add_parser("step3", help="只跑步骤 3：超音域修正 + 最终导出")
    p3.add_argument("--melody", default=None, help="主旋律 MIDI / JSON（默认 out/main_melody.mid）")
    p3.add_argument("--chords", default=None, help="和弦 MIDI / JSON（默认 out/chords_merged.mid）")
    p3.add_argument("--out", "-o", required=True, help="输出目录")
    p3.add_argument("--name", default=None, help="最终 MIDI 文件名")
    p3.add_argument("--low", type=int, default=40, help="最低音（默认 40）")
    p3.add_argument("--high", type=int, default=88, help="最高音（默认 88）")
    p3.add_argument("--instrument", dest="instrument_mode", default="acoustic",
                    choices=["acoustic", "electric"],
                    help="吉他音色适配：acoustic 木吉他（默认）/ electric 电吉他")

    p_paths = sub.add_parser("paths", help="显示生效的路径与设备")
    p_paths.add_argument("--probe", action="store_true", help="同时探测推理设备")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    reset_cancel()

    try:
        if args.command == "paths":
            print_paths()
            if args.probe:
                info = probe_device(force=True)
                print(f"[设备] 最佳：{info['best']}  名称：{info.get('name') or '-'}  "
                      f"torch：{info.get('torch') or '-'}")
                if info.get("error"):
                    print(f"[设备] 探测提示：{info['error']}")
            return 0

        if args.command == "run":
            run_all(args.audio, args.out, device=args.device, dtype=args.dtype,
                    merge_mode=args.merge_mode, low=args.low, high=args.high,
                    instrument_mode=args.instrument_mode,
                    max_seconds=args.max_seconds)
            return 0

        if args.command == "step1":
            step1(args.input, args.out, device=args.device, dtype=args.dtype,
                  max_seconds=args.max_seconds)
            return 0

        if args.command == "step2":
            step2(args.out, vocal=args.vocal, instrumental=args.instrumental,
                  chords=args.chords, integrated=args.integrated,
                  merge_mode=args.merge_mode)
            return 0

        if args.command == "step3":
            step3(args.melody, args.out, chords=args.chords,
                  low=args.low, high=args.high,
                  instrument_mode=args.instrument_mode, final_name=args.name)
            return 0

    except KeyboardInterrupt:
        cancel_current()
        print("已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ != "__main__":
    # 以包方式导入时，让顶层的 `import pipeline` 也拿到同一个对象
    sys.modules.setdefault("pipeline", sys.modules[__name__])

if __name__ == "__main__":
    raise SystemExit(main())
