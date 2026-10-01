#!/usr/bin/env python3
"""Repair the I/Q decimation filter uploads on their way to the device.

SmDevice::ConfigureIQStreamingNet in the macOS build fills each decimation
filter stage with DSP::GetLowpassFIRTaps_64f, which there zeroes the array and
writes a single 1.0 at the centre: a unit impulse, not a low-pass.

The Linux builds design the same four stages at run time with the real
GetLowpassFIRTaps_64f, and the stage chosen by the decimation follows the
requested bandwidth. stage_cutoffs, design_taps and encode_coeffs below
reproduce that bit for bit (checked by reference/check_design.py), and the
repair uses them to replace each impulse with the filter Linux would send for
the same settings. That needs nothing from a Linux .so.

The Linux builds also contain constant tables for the four stages, but those
go into a command list the streaming engine never sends. They can still be
extracted and substituted instead, as an alternative for comparison; they are
not embedded here, since they are Signal Hound's data.

WriteIQFilter command layout, recovered from the binary:

    word 0      (0x04 << 24) | (payload_words << 16) | stage_address
    words 1-8   zero
    words 9..   (n+1)/2 int32 coefficients, edge to centre inclusive

Only half the filter is sent because the taps are symmetric. Coefficients are
the taps divided by the sum of all n, times a per-stage scale, truncated to
int32. An impulse upload therefore ends on a bare scale value with nothing but
zeros before it.

    python3 sm_filters.py selftest [filter_tables.json]
    python3 sm_filters.py extract ./libsm_api.so.2.3.9     # optional, for tables mode
"""

import json
import math
import os
import struct
import sys
from fractions import Fraction

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


# ---- the filters Linux actually sends --------------------------------------
#
# Cutoff logic of SmDevice::ConfigureIQStreamingNet. It is the same in the
# macOS 2.3.7 and Linux 2.3.9 builds, constants included. Cutoffs are in
# cycles per sample at each stage's input.

BASE_RATE = {0: 200e6, 1: 122.88e6}   # GetSampleRateNet: native, LTE
DEFAULT_FC = (0.08, 0.2, 0.2, 0.2)
FC_MIN = 0.02
FC_MAX = (0.083325, 0.2, 0.2, 0.225)


def _f32(x):
    """Round to single precision: the design function takes its cutoff as a float."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def stage_cutoffs(rate_mode, decimation, bandwidth):
    """The four cutoffs ConfigureIQStreamingNet passes to the design function.

    rate_mode is SmIQStreamSampleRate (0 native, 1 LTE). bandwidth is what was
    given to smSetIQBandwidth. The library first clamps it to between 1% and
    82.5% of the output rate. Then, at decimation 1, 2, 4 or 8, the last stage
    in use is narrowed to bandwidth * 1.02 / 2 and clamped to [0.02, max]; at
    any other decimation all four keep their defaults. Arithmetic follows the
    binary operation by operation so the results round the same way.

    The library skips the narrowing at decimation 4 and 8 if a flag beside the
    decimation is set, but smSetIQBandwidth always writes 0 there on the
    networked path, so it is not modelled.
    """
    base = BASE_RATE[rate_mode]
    rate = base / float(decimation)
    bw = bandwidth
    lo = rate * 0.01
    hi = rate * 0.825
    if bw < lo:
        bw = lo
    elif bw > hi:
        bw = hi

    fc = list(DEFAULT_FC)
    wide = bw * 1.02
    stage = {1: 0, 2: 1, 4: 2, 8: 3}.get(decimation)
    if stage is not None:
        if decimation == 1:
            c = wide / 1e9 * 0.5            # stage 1 input, whatever the base rate
        elif decimation == 2:
            c = wide / base * 0.5
        elif decimation == 4:
            c = wide / (base * 0.5) * 0.5
        else:
            c = wide / (base * 0.25) * 0.5
        if c < FC_MIN:
            c = FC_MIN
        elif c > FC_MAX[stage]:
            c = FC_MAX[stage]
        fc[stage] = c
    return [_f32(c) for c in fc]


def _fma(a, b, c):
    """a * b + c with one rounding, as the aarch64 fmadd/fmsub do."""
    return float(Fraction(a) * Fraction(b) + Fraction(c))


def _blackman(n):
    """DSP::GetBlackmanWindow, including its fused multiply-adds."""
    span = float(n - 1)
    out = []
    for i in range(n):
        k = float(i)
        w = _fma(-math.cos(k * 6.283185307179586 / span), 0.49656, 0.42659)
        out.append(_fma(math.cos(k * 12.566370614359172 / span), 0.076849, w))
    return out


def design_taps(n, fc):
    """DSP::GetLowpassFIRTaps_64f(taps, n, fc, Blackman, normalise=true)."""
    fc = _f32(fc)
    w = (fc + fc) * 3.141592653589793
    half = n // 2
    taps = []
    for k in range(-half, n - half):
        x = float(k) * w
        taps.append(1.0 if x == 0.0 else math.sin(x) / x)
    taps = [t * v for t, v in zip(taps, _blackman(n))]
    total = 0.0
    for t in taps:
        total += t
    return [t / total for t in taps]


def encode_coeffs(stage, taps):
    """The coefficient words CommandList::WriteIQFilter sends for one stage.

    Multiplies by the reciprocal of the sum rather than dividing by it, which
    can differ from encode_half in the last bit before truncation.
    """
    scale = STAGES[stage][1]
    total = 0.0
    for t in taps:
        total += t
    inv = 1.0 / total
    return [int((inv * t) * scale) for t in taps[:(len(taps) + 1) // 2]]


def encode_half(taps, scale):
    """Match WriteIQFilter: taps over the full sum, times scale, int32, first half."""
    total = sum(taps)
    return [int(t / total * scale) for t in taps[: (len(taps) + 1) // 2]]


def repair_packet(words, tables=None, taps_for=None):
    """Replace impulse filter uploads in one command packet's words.

    taps_for(stage) returns (taps, cutoff) for the filter to send, or None if
    it cannot say; it is used in preference to tables, which maps tap count to
    an extracted constant table. Only an impulse is touched. A packet already
    carrying real coefficients, for instance from a fixed future build, passes
    through unchanged. Returns (words, report); report entries for impulses
    say whether they were repaired and, if not, why.
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
            fixed = None
            if taps_for is not None:
                got = taps_for(stage)
                if got is None:
                    entry["reason"] = "settings unavailable"
                else:
                    taps, fc = got
                    fixed = encode_coeffs(stage, taps)
                    entry["fc"] = fc
            elif tables is not None and n in tables:
                fixed = encode_half(tables[n], scale)
            else:
                entry["reason"] = f"no {n}-tap table"
            entry["repaired"] = fixed is not None
            if fixed is not None:
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


def _design_for(stage):
    """taps_for for the self test: the library's defaults at decimation 8, 80% bandwidth."""
    fc = stage_cutoffs(0, 8, 25e6 * 0.8)[stage - 1]
    return design_taps(STAGES[stage][0], fc), fc


def _self_test(tables):
    ok = True
    sources = [("design", {"taps_for": _design_for})]
    if tables is not None:
        sources.append(("tables", {"tables": tables}))
    for label, source in sources:
        print(f"== {label}")
        ok = _self_test_one(source) and ok
    print("\nself test:", "pass" if ok else "FAIL")
    return ok


def _self_test_one(source):
    ok = True
    for stage, (n, scale, addr) in STAGES.items():
        cmd = _impulse_command(stage)
        # surround with other command words to mimic a real packet
        packet = [0x04010001, 0x00000001] + cmd + [0x04010022, 0x000000FF]
        fixed, report = repair_packet(packet, **source)
        half = (n + 1) // 2
        coeffs = fixed[2 + 9: 2 + 9 + half]
        peak = max(coeffs)
        print(f"stage {stage}: {n} taps, addr {addr:#x}, header {cmd[0]:#010x}")
        print(f"  impulse centre {scale} ({scale.bit_length()} bits) -> "
              f"repaired centre {coeffs[-1]} ({coeffs[-1].bit_length()} bits)")
        checks = {
            "detected": report and report[0]["impulse"],
            "repaired": report and report[0].get("repaired"),
            "neighbours untouched": fixed[:2] == packet[:2] and fixed[-2:] == packet[-2:],
            "header and zero block untouched": fixed[2:11] == packet[2:11],
            "centre is the peak": coeffs[-1] == peak,
            "fits below the impulse": peak.bit_length() < scale.bit_length(),
            "idempotent": repair_packet(fixed, **source)[1][0]["impulse"] is False,
        }
        for name, passed in checks.items():
            if not passed:
                print(f"  ! {name} FAILED")
                ok = False
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
        tables = load_tables(path) if os.path.exists(path) else None
        return 0 if _self_test(tables) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
