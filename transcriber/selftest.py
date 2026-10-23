# -*- coding: utf-8 -*-
"""《异键演奏》本地自检脚本。

用合成的 MIDI 覆盖需求里最容易出错的地方：
  1. 三个分轨 / 一个综合 MIDI 两种输入方式；
  2. 不同 BPM 的文件也按“物理秒”对齐；
  3. 和弦轨 Chords1~Chords5 全部遍历，30ms 内的起音合成一个切片；
  4. Vocal 与 Ins 重叠时保留更长 / 力度更高的音，结果严格单音；
  5. 孤立倍频噪点（A6=93 类）与超音域音符被拉回 40~88；
  6. 最终 MIDI 只有 Main Melody + Chords 两轨。

用法：
    python selftest.py            # 全部自检，打印 PASS / FAIL
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pretty_midi  # noqa: E402

try:
    from . import pipeline  # noqa: E402
    from . import convert as _convert  # noqa: E402
except ImportError:            # 直接运行本文件
    import pipeline  # noqa: E402,F401
    import convert as _convert  # noqa: E402,F401

sys.modules.setdefault("convert", _convert)

from convert import (  # noqa: E402
    Note,
    TrackNotes,
    load_chords_json,
    monophonic_violations,
    read_midi_tracks,
    write_midi,
)

WORK = HERE / "out" / "_selftest"


def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  [ok] {message}")


def _build_inputs() -> None:
    """生成测试用的三个分轨 + 一个综合 MIDI（故意使用不同 BPM）。"""
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "separate").mkdir(parents=True)
    (WORK / "integrated").mkdir(parents=True)

    # ---- 人声：正常音 + 一颗短促的 93（A6，典型倍频噪点）
    vocal = TrackNotes("Vocal", [
        Note(72, 0.00, 1.00, 90),
        Note(74, 2.50, 3.00, 90),
        Note(93, 3.05, 3.25, 100),   # 0.20s，前后是 74 / 76 -> 应 -12
        Note(76, 3.50, 4.00, 90),
        Note(35, 4.00, 4.50, 90),    # 偏低 -> +12
    ])
    # ---- 器乐：与第 1 颗人声音重叠且更长 -> 合并后保留它
    ins = TrackNotes("Ins", [
        Note(67, 0.50, 2.00, 100),
        Note(95, 5.00, 5.50, 90),    # 偏高但不孤立 -> 兜底 -12
    ])
    # ---- 和弦：5 条轨，前 3 颗在 30ms 内，后 3 颗在 30ms 内
    chords = [
        TrackNotes("Chords1", [Note(48, 1.00, 2.00, 80), Note(52, 1.02, 2.00, 80)]),
        TrackNotes("Chords2", [Note(55, 1.025, 2.00, 80)]),
        TrackNotes("Chords3", [Note(60, 2.50, 3.50, 80), Note(64, 2.51, 3.50, 80)]),
        TrackNotes("Chords4", [Note(67, 2.52, 3.50, 80)]),
        TrackNotes("Chords5", [Note(72, 2.52, 3.50, 80)]),
    ]

    write_midi(WORK / "separate" / "melody_vocal.mid", [vocal], bpm=120)
    write_midi(WORK / "separate" / "melody_instrumental.mid", [ins], bpm=100)
    write_midi(WORK / "separate" / "chords.mid", chords, bpm=90)
    write_midi(WORK / "integrated" / "transcription.mid",
               [vocal, ins] + chords, bpm=120)


def _check_step2(step2_result: dict, tag: str) -> None:
    print(f"- 步骤 2（{tag}）")
    _expect(step2_result["counts"]["chord_slices"] == 2,
            f"{tag}: 2 个和弦切片（实际 {step2_result['counts']['chord_slices']}）")
    slices = load_chords_json(WORK / "out" / "chords.json")
    _expect([sorted(s.pitches) for s in slices] == [[48, 52, 55], [60, 64, 67, 72]],
            f"{tag}: 30ms 内的和弦音被聚合，音高正确")
    _expect(abs(slices[0].start_sec - 1.00) < 0.01,
            f"{tag}: 90 BPM 文件也按物理秒对齐（{slices[0].start_sec:.4f}s ≈ 1.000s）")

    melody = read_midi_tracks(WORK / "out" / "main_melody.mid")
    _expect(len(melody) == 1 and melody[0].name == "Main Melody",
            f"{tag}: 输出一条 Main Melody 轨")
    notes = melody[0].notes
    _expect(len(notes) == 6, f"{tag}: 合并后 6 颗单音（实际 {len(notes)}）")
    _expect(not monophonic_violations(notes), f"{tag}: 主旋律严格单音、无重叠")
    _expect(any(abs(n.pitch - 67) < 0.1 and abs(n.start_sec - 0.5) < 0.01 for n in notes),
            f"{tag}: 重叠时保留了更长的 67 号音")


def _check_step3(step3_result: dict, tag: str) -> None:
    print(f"- 步骤 3（{tag}）")
    pm = pretty_midi.PrettyMIDI(str(step3_result["outputs"]["final_midi"]))
    names = [i.name for i in pm.instruments]
    _expect(names == ["Main Melody", "Chords"], f"{tag}: 最终 MIDI 只有两轨 {names}")
    melody = [n for n in pm.instruments[0].notes]
    chords = [n for n in pm.instruments[1].notes]
    _expect(len(melody) == 6, f"{tag}: 主旋律 6 颗音")
    _expect(all(40 <= n.pitch <= 88 for n in melody), f"{tag}: 主旋律音高全部落在 40~88")
    _expect(not monophonic_violations([Note(n.pitch, n.start, n.end, n.velocity)
                                       for n in melody]), f"{tag}: 最终主旋律仍严格单音")
    _expect(len(chords) == 7, f"{tag}: 和弦 7 颗音（3 + 4）")

    pitches_by_start = {}
    for n in melody:
        pitches_by_start[round(n.start, 2)] = n.pitch
    _expect(pitches_by_start.get(3.05) == 81, f"{tag}: A6=93 的短噪点被拉回 81")
    _expect(pitches_by_start.get(4.0) == 47, f"{tag}: 35 的偏低音被拉回 47")
    _expect(pitches_by_start.get(5.0) == 83, f"{tag}: 95 的超界长音被兜底拉回 83")

    # 整体偏高：三颗 89/90/91 应整轨 -12
    shifted, stats = pipeline.fix_pitch_range(
        [Note(89, 0.0, 1.0), Note(90, 1.0, 2.0), Note(91, 2.0, 3.0)], low=40, high=88)
    _expect([n.pitch for n in shifted] == [77, 78, 79] and stats["global_down_octaves"] == 1,
            f"{tag}: 整体偏高时整轨降一个八度")

    # 孤立噪点：中低音区里一颗 0.2s 的 93 -> 只把它自己 -12
    spike, _ = pipeline.fix_pitch_range(
        [Note(60, 0.0, 1.0), Note(62, 1.0, 2.0),
         Note(93, 2.05, 2.25), Note(64, 2.5, 3.5)], low=40, high=88)
    _expect([n.pitch for n in spike] == [60, 62, 81, 64],
            f"{tag}: 孤立 93 只降自己、不拖累整轨")


def main() -> int:
    print("=" * 60)
    print("《异键演奏》本地自检")
    print("=" * 60)
    _build_inputs()

    out = WORK / "out"

    print("- 步骤 2（三个分轨）")
    shutil.rmtree(out, ignore_errors=True)
    r2 = pipeline.step2(out,
                        vocal=WORK / "separate" / "melody_vocal.mid",
                        instrumental=WORK / "separate" / "melody_instrumental.mid",
                        chords=WORK / "separate" / "chords.mid")
    _check_step2(r2, "分轨输入")

    print("- 步骤 2（综合 MIDI）")
    shutil.rmtree(out, ignore_errors=True)
    r2i = pipeline.step2(out, integrated=WORK / "integrated" / "transcription.mid")
    _check_step2(r2i, "综合输入")

    r3 = pipeline.step3(out / "main_melody.mid", out,
                        chords=out / "chords_merged.mid", source_audio="test.wav")
    _check_step3(r3, "导出")

    print("=" * 60)
    print("全部自检通过。")
    print("=" * 60)
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ != "__main__":
    # 以包方式导入时，让顶层的 `import selftest` 也拿到同一个对象
    sys.modules.setdefault("selftest", sys.modules[__name__])

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n[自检失败] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[自检异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
