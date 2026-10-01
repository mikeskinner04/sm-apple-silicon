"""Check sm_filters' port of the Linux SM API filter design against ref_filters.json.

sm_filters.design_taps ports DSP::GetLowpassFIRTaps_64f (windowed sinc,
Blackman, normalised) and sm_filters.encode_coeffs ports the encoding in
CommandList::WriteIQFilter, both from the aarch64 2.3.9 build. This compares
them bit for bit with what the library itself produced. Run it on the Mac:
sin and cos come from the platform libm, which is the one thing that could
differ from glibc.

    python3 reference/check_design.py ref_filters.json
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sm_filters  # noqa: E402


def as_double(hex_bits):
    return struct.unpack(">d", bytes.fromhex(hex_bits))[0]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "ref_filters.json"
    ref = json.load(open(path))
    taps_total = taps_bad = enc_bad = full_bad = 0
    for f in ref["filters"]:
        theirs = [as_double(h) for h in f["coeffs"]]
        ours = sm_filters.design_taps(f["taps"], f["fc"])
        taps_total += len(theirs)
        taps_bad += sum(a != b for a, b in zip(theirs, ours))
        words = [w - (1 << 32) if w >= 1 << 31 else w for w in f["command"][9:]]
        enc_bad += words != sm_filters.encode_coeffs(f["stage"], theirs)
        full_bad += words != sm_filters.encode_coeffs(f["stage"], ours)
    n = len(ref["filters"])
    print(f"API {ref['api_version']}: {n} filters")
    print(f"taps differing: {taps_bad} of {taps_total}")
    print(f"encoding of the library's own taps: {enc_bad} of {n} commands differ")
    print(f"design and encoding together: {full_bad} of {n} commands differ")
    return 1 if full_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
