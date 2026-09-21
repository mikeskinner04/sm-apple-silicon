#!/usr/bin/env python3
"""Repair the I/Q decimation filter uploads on their way to the device.

SmDevice::ConfigureIQStreamingNet in the macOS build fills each decimation
filter stage with DSP::GetLowpassFIRTaps_64f, which there zeroes the array and
writes a single 1.0 at the centre: a unit impulse, not a low-pass. The aarch64
Linux build of the same library writes these stages from constant tables
instead, as does the x86-64 build, with identical values. So the correct
coefficients exist in Signal Hound's own shipped code.

This module reads those tables out of your local copy of either Linux .so and
substitutes them into outgoing command packets. The tables are not embedded
here, since they are Signal Hound's data.

WriteIQFilter command layout, recovered from the binary:

    word 0      (0x04 << 24) | (payload_words << 16) | stage_address
    words 1-8   zero
    words 9..   (n+1)/2 int32 coefficients, edge to centre inclusive

Only half the filter is sent because the taps are symmetric. Coefficients are
the taps divided by the sum of all n, times a per-stage scale, truncated to
int32. An impulse upload therefore ends on a bare scale value with nothing but
zeros before it.

    python3 sm_filters.py extract ./libsm_api.so.2.3.9     # writes filter_tables.json
    python3 sm_filters.py selftest [filter_tables.json]
"""

import json
import os
import struct
import sys

# stage -> (tap count, scale, address in the WriteIQFilter header)
STAGES = {
    1: (189, 1 << 19, 0x478),
    2: (79, 1 << 18, 0x578),
    3: (159, 1 << 18, 0x678),
    4: (159, 1 << 18, 0x778),
}
ADDR_TO_STAGE = {addr: stage for stage, (_, _, addr) in STAGES.items()}
TABLES_FILE = "filter_tables.json"


def extract_tables(so_path):
    """Find the shipped coefficient tables by content.

    Each is a run of n doubles, perfectly symmetric, no zeros, all within
    [-1, 1], with exactly 1.0 at the centre. A large table also contains
    smaller symmetric windows around the same centre, so keep only the largest
    match per centre.
    """
    data = open(so_path, "rb").read()
    by_centre = {}
    for off in range(0, len(data) - 8, 8):
        if struct.unpack_from("<d", data, off)[0] != 1.0:
            continue
        for n in sorted({n for n, _, _ in STAGES.values()}, reverse=True):
            half = n // 2
            start = off - half * 8
            if start < 0 or start + n * 8 > len(data):
                continue
            t = struct.unpack_from(f"<{n}d", data, start)
            if any(x == 0 or abs(x) > 1.0 for x in t):
                continue
            if all(t[i] == t[n - 1 - i] for i in range(half)):
                by_centre[off] = list(t)
                break                                  # largest wins
    tables = {}
    for t in by_centre.values():
        tables.setdefault(len(t), []).append(t)
    want = sorted({n for n, _, _ in STAGES.values()})
    for n in want:
        if len(tables.get(n, [])) != 1:
            raise RuntimeError(f"expected one {n}-tap table, found "
                               f"{len(tables.get(n, []))}; is this a Linux SM API .so?")
    return {n: tables[n][0] for n in want}


def encode_half(taps, scale):
    """Match WriteIQFilter: taps over the full sum, times scale, int32, first half."""
    total = sum(taps)
    return [int(t / total * scale) for t in taps[: (len(taps) + 1) // 2]]


def repair_packet(words, tables):
    """Replace impulse filter uploads in one command packet's words.

    Only an impulse is touched. A packet already carrying real coefficients,
    for instance from a fixed future build, passes through unchanged.
    Returns (words, report).
    """
    out = list(words)
    report = []
    i = 0
    while i < len(out):
        w = out[i]
        stage = ADDR_TO_STAGE.get(w & 0xFFFF)
        if (w >> 24) != 0x04 or stage is None:
            i += 1
            continue
        n, scale, _ = STAGES[stage]
        half = (n + 1) // 2
        payload_words = (w >> 16) & 0xFF
        start = i + 9
        end = start + half
        if payload_words != half + 8 or end > len(out) or any(out[i + 1:start]):
            i += 1                                     # not a WriteIQFilter we recognise
            continue
        coeffs = out[start:end]
        is_impulse = coeffs[-1] == scale and not any(coeffs[:-1])
        entry = {"stage": stage, "taps": n, "word": i, "impulse": is_impulse}
        if is_impulse:
            fixed = encode_half(tables[n], scale)
            out[start:end] = fixed
            entry.update(centre=fixed[-1], edge=fixed[0])
        report.append(entry)
        i = end
    return out, report


def load_tables(path=TABLES_FILE):
    with open(path) as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


def _impulse_command(stage):
    """Build a WriteIQFilter command exactly as the macOS build would send it."""
    n, scale, addr = STAGES[stage]
    half = (n + 1) // 2
    header = (0x04 << 24) | ((half + 8) << 16) | addr
    coeffs = [0] * half
    coeffs[-1] = scale
    return [header] + [0] * 8 + coeffs


def _self_test(tables):
    ok = True
    for stage, (n, scale, addr) in STAGES.items():
        cmd = _impulse_command(stage)
        # surround with other command words to mimic a real packet
        packet = [0x04010001, 0x00000001] + cmd + [0x04010022, 0x000000FF]
        fixed, report = repair_packet(packet, tables)
        half = (n + 1) // 2
        coeffs = fixed[2 + 9: 2 + 9 + half]
        peak = max(coeffs)
        print(f"stage {stage}: {n} taps, addr {addr:#x}, header {cmd[0]:#010x}")
        print(f"  impulse centre {scale} ({scale.bit_length()} bits) -> "
              f"shipped centre {coeffs[-1]} ({coeffs[-1].bit_length()} bits)")
        checks = {
            "detected": report and report[0]["impulse"],
            "neighbours untouched": fixed[:2] == packet[:2] and fixed[-2:] == packet[-2:],
            "header and zero block untouched": fixed[2:11] == packet[2:11],
            "centre is the peak": coeffs[-1] == peak,
            "fits below the impulse": peak.bit_length() < scale.bit_length(),
            "idempotent": repair_packet(fixed, tables)[1][0]["impulse"] is False,
        }
        for name, passed in checks.items():
            if not passed:
                print(f"  ! {name} FAILED")
                ok = False
    print("\nself test:", "pass" if ok else "FAIL")
    return ok


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "extract":
        tables = extract_tables(sys.argv[2])
        with open(TABLES_FILE, "w") as f:
            json.dump({str(k): v for k, v in tables.items()}, f)
        for n, t in sorted(tables.items()):
            print(f"{n:4d} taps  sum {sum(t):.6f}  centre {t[n // 2]}")
        print(f"wrote {os.path.abspath(TABLES_FILE)}")
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        path = sys.argv[2] if len(sys.argv) > 2 else TABLES_FILE
        return 0 if _self_test(load_tables(path)) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
