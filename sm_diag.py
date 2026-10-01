#!/usr/bin/env python3
"""SM200C on macOS: diagnostic harness.

Runs a battery of experiments on each backend, Python and native, each in a
process of its own, and writes one report.json plus a self-contained
report.html covering both. Every I/Q experiment gets pass/fail checks and
charts: a zero map across the whole capture, an I/Q trace, and a spectrogram.

    python3 sm_diag.py ./libsm_api.2.3.7.dylib --list
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --all
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --all --backends native
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --only sweep,iq-dec8-short
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --interactive

Every experiment is isolated: a failure is recorded and the run carries on.
Needs numpy.
"""

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import numpy as np
except ImportError:
    sys.exit("sm_diag needs numpy for its checks and charts: pip3 install numpy")
import sm_charts
import sm_transport as T

# ---- sm_api.h constants -----------------------------------------------------
SM_AUTO_ATTEN = -1
smDataType32fc, smDataType16sc = 0, 1
smModeIdle, smModeSweeping, smModeRealTime, smModeIQStreaming = 0, 1, 2, 3
smIQStreamSampleRateNative, smIQStreamSampleRateLTE = 0, 1
smFalse, smTrue = 0, 1
smPowerStateOn, smPowerStateStandby = 0, 1
smDetectorAverage, smDetectorMinMax = 0, 1
smScaleLog = 0
smVideoLog = 0
smWindowFlatTop = 0
smSweepSpeedAuto, smSweepSpeedNormal, smSweepSpeedFast = 0, 1, 2
SM_SYNC_ERR = -11             # smSyncErr: aux block missing where expected
NETWORKED_BASE_RATE = 200e6
DEVICE_TYPES = {0: "SM200A", 1: "SM200B", 2: "SM200C", 3: "SM435B", 4: "SM435C"}


def bind(lib):
    ci, cd, cf = ctypes.c_int, ctypes.c_double, ctypes.c_float
    p, cc = ctypes.POINTER, ctypes.c_char_p
    lib.smGetErrorString.restype = cc
    lib.smGetErrorString.argtypes = [ci]
    lib.smGetAPIVersion.restype = cc
    lib.smOpenNetworkedDevice.argtypes = [p(ci), cc, cc, ctypes.c_uint16]
    for name, argtypes in {
        "smCloseDevice": [ci],
        "smAbort": [ci],
        "smPreset": [ci],
        "smGetDeviceInfo": [ci, p(ci), p(ci)],
        "smGetFirmwareVersion": [ci, p(ci), p(ci), p(ci)],
        "smGetDeviceDiagnostics": [ci, p(cf), p(cf), p(cf)],
        "smGetFullDeviceDiagnostics": [ci, ctypes.c_void_p],
        "smGetSFPDiagnostics": [ci, p(cf), p(cf), p(cf), p(cf)],
        "smGetCalDate": [ci, p(ctypes.c_uint64)],
        "smSetPowerState": [ci, ci],
        "smGetPowerState": [ci, p(ci)],
        "smGetReference": [ci, p(ci)],
        "smSetReference": [ci, ci],
        "smGetGPSState": [ci, p(ci)],
        "smSetAttenuator": [ci, ci],
        "smGetAttenuator": [ci, p(ci)],
        "smSetRefLevel": [ci, cd],
        "smSetPreselector": [ci, ci],
        "smGetPreselector": [ci, p(ci)],
        "smNetworkedSpeedTest": [ci, cd, p(cd)],
        # I/Q streaming
        "smSetIQBaseSampleRate": [ci, ci],
        "smSetIQDataType": [ci, ci],
        "smSetIQCenterFreq": [ci, cd],
        "smGetIQCenterFreq": [ci, p(cd)],
        "smSetIQSampleRate": [ci, ci],
        "smSetIQBandwidth": [ci, ci, cd],
        "smSetIQQueueSize": [ci, cf],
        "smGetIQParameters": [ci, p(cd), p(cd)],
        "smGetIQCorrection": [ci, p(cf)],
        "smGetIQ": [ci, ctypes.c_void_p, ci, p(cd), ci, p(ctypes.c_int64), ci,
                    p(ci), p(ci)],
        # sweep
        "smSetSweepSpeed": [ci, ci],
        "smSetSweepCenterSpan": [ci, cd, cd],
        "smSetSweepCoupling": [ci, cd, cd, cd],
        "smSetSweepDetector": [ci, ci, ci],
        "smSetSweepScale": [ci, ci],
        "smSetSweepWindow": [ci, ci],
        "smGetSweepParameters": [ci, p(cd), p(cd), p(cd), p(cd), p(ci)],
        "smGetSweep": [ci, p(cf), p(cf), p(ctypes.c_int64)],
        "smConfigure": [ci, ci],
        "smGetCurrentMode": [ci, p(ci)],
    }.items():
        getattr(lib, name).argtypes = argtypes


class Api:
    """Thin wrapper that records every call and never raises on device errors."""

    def __init__(self, lib):
        self.lib = lib
        self.log = []

    def __call__(self, name, *args):
        status = getattr(self.lib, name)(*args)
        if status != 0:
            self.log.append({
                "call": name,
                "status": status,
                "message": self.lib.smGetErrorString(status).decode(),
            })
        return status

    def err(self, status):
        return self.lib.smGetErrorString(status).decode()


# ---- aux block decoding -----------------------------------------------------
# Offsets recovered from AuxStatusReader::Update in libsm_api.2.3.7.dylib.
def decode_aux(block_hex):
    b = bytes.fromhex(block_hex)
    if len(b) < 0x48:
        return {"error": f"aux block only {len(b)} bytes"}
    status = struct.unpack_from("<I", b, 0x1c)[0]
    return {
        "status_word": f"0x{status:08x}",
        "bit0": status & 1,
        "bits1_4": (status >> 1) & 0xF,
        "bit5": (status >> 5) & 1,
        "bit6": (status >> 6) & 1,
        "sync_counter": (status >> 8) & 0xFF,   # mismatch here is a framing fault
        "word_0x22": struct.unpack_from("<H", b, 0x22)[0],
        "word_0x24": struct.unpack_from("<I", b, 0x24)[0],
        "word_0x2c": struct.unpack_from("<I", b, 0x2c)[0],
        "raw_first_64": b[:64].hex(" "),
        "all_zero": not any(b),
    }


def summarise_commands(commands):
    """Opcode is the top byte of each 32-bit word. 0x13 initiate/stop streaming,
    0x23 fetch streaming I/Q, 0x04 fragmentation."""
    opcodes = {}
    for words in commands:
        for w in words:
            op = (w >> 24) & 0xFF
            opcodes[op] = opcodes.get(op, 0) + 1
    return {
        "packets": len(commands),
        "opcode_counts": {f"0x{k:02x}": v for k, v in sorted(opcodes.items())},
        "first_packets": [[f"0x{w:08x}" for w in c[:16]] for c in commands[:6]],
    }


# ---- experiments ------------------------------------------------------------
def segment_samples(decimation):
    """Output samples carried by one 8 KB datagram: 2048 at the hardware rate,
    fewer once the host decimates further."""
    hw = min(decimation, 8)
    return max(16, 2048 * hw // decimation)


def run_iq(api, dev, transport, cfg, seconds, keep_samples):
    """Configure I/Q streaming, pull samples, report what actually arrived.

    cfg["repair"], if present, overrides filter repair for this one experiment:
    False forces it off, True keeps the session's mode, and "design" or
    "tables" selects that source. This is what lets an A/B run in a single
    session. The transport reads the mode at packet time, so changing it here
    takes effect for this experiment's uploads and is restored afterwards.
    """
    short = cfg.get("short", False)
    bps = 4 if short else 8
    result = {}

    repair = transport.repair
    saved_mode = repair.mode if repair else None
    want = cfg.get("repair")
    if repair and want is False:
        repair.mode = "off"
    elif repair and isinstance(want, str):
        if repair.available(want):
            repair.mode = want
        else:
            result["repair_note"] = f"repair {want} requested but not available"
    elif want and (not repair or repair.mode == "off"):
        result["repair_note"] = "repair forced on but none is available"
    result["repair_mode"] = repair.mode if repair else "off"
    result["repair_active"] = result["repair_mode"] != "off"

    api("smAbort", dev)
    api("smSetIQBaseSampleRate", dev, cfg.get("base_rate", smIQStreamSampleRateNative))
    api("smSetIQDataType", dev, smDataType16sc if short else smDataType32fc)
    api("smSetIQCenterFreq", dev, cfg.get("center", 1e9))
    api("smSetIQSampleRate", dev, cfg["decimation"])
    if cfg.get("bandwidth") is not False:
        api("smSetIQBandwidth", dev, smFalse,
            NETWORKED_BASE_RATE / cfg["decimation"] * 0.8)
    if cfg.get("atten") is not None:
        api("smSetAttenuator", dev, cfg["atten"])
    else:
        api("smSetAttenuator", dev, SM_AUTO_ATTEN)
        api("smSetRefLevel", dev, cfg.get("ref_level", -20.0))
    if cfg.get("preselector") is not None:
        api("smSetPreselector", dev, cfg["preselector"])
    if cfg.get("queue_ms"):
        api("smSetIQQueueSize", dev, cfg["queue_ms"])

    transport.snapshot()                      # discard setup-phase counters
    t0 = time.monotonic()
    status = api("smConfigure", dev, smModeIQStreaming)
    result["configure_ms"] = round((time.monotonic() - t0) * 1e3, 1)
    result["configure_status"] = status
    if status < 0:
        result["error"] = api.err(status)
        result["transport"] = transport.snapshot()
        if repair:
            repair.mode = saved_mode
        return result

    rate, bw, actual = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    api("smGetIQParameters", dev, ctypes.byref(rate), ctypes.byref(bw))
    api("smGetIQCenterFreq", dev, ctypes.byref(actual))
    scale = ctypes.c_float()
    api("smGetIQCorrection", dev, ctypes.byref(scale))
    result.update(sample_rate=rate.value, bandwidth=bw.value,
                  center_actual=actual.value, correction=scale.value)

    # Blocks of about 5 ms, so per-call overhead stays small at high rates.
    block = 32768
    while block < rate.value * 0.005 and block < (1 << 20):
        block <<= 1
    total = max(block, int(rate.value * cfg.get("seconds", seconds)))
    seg = segment_samples(cfg["decimation"])
    buf = ctypes.create_string_buffer(block * bps)
    view = np.frombuffer(buf, dtype=np.uint8)
    ns, loss, remaining = ctypes.c_int64(), ctypes.c_int(), ctypes.c_int()

    captured, losses, nonzero, first_ns, sync_flags = 0, 0, 0, None, 0
    peak = 0
    keep = bytearray()
    zero_runs, loss_at, sync_at = [], [], []
    segments = 0
    ts_err = None
    t_first = None
    first_n = 0
    first = True
    t0 = time.monotonic()
    while captured < total:
        n = min(block, total - captured)
        status = api("smGetIQ", dev, buf, n, None, 0, ctypes.byref(ns),
                     smTrue if first else smFalse, ctypes.byref(loss),
                     ctypes.byref(remaining))
        if status == SM_SYNC_ERR:
            # GetIQ copies the samples before it checks the sync flag, so they
            # were delivered; the flag says the aux block of some transfer was
            # not where it belonged. Count it and carry on.
            sync_flags += 1
            sync_at.append(captured)
        elif status < 0:
            result["getiq_error"] = api.err(status)
            break
        if first:
            first_ns = ns.value
            t_first = time.monotonic()
            first_n = n
        elif first_ns and rate.value:
            err = abs(ns.value - (first_ns + captured / rate.value * 1e9))
            ts_err = err if ts_err is None else max(ts_err, err)
        data = view[:n * bps]

        # Zero map: which datagram-sized stretches came back as exact zeros.
        nseg = n // seg
        whole = nseg * seg * bps
        per_seg = np.count_nonzero(data[:whole].reshape(nseg, seg * bps), axis=1)
        nonzero += int(per_seg.sum()) + int(np.count_nonzero(data[whole:]))
        zero = per_seg == 0
        if zero.any():
            edges = np.flatnonzero(np.diff(np.concatenate(([0], zero.view(np.int8), [0]))))
            for a, b in zip(edges[0::2], edges[1::2]):
                start = segments + int(a)
                if zero_runs and zero_runs[-1][0] + zero_runs[-1][1] == start:
                    zero_runs[-1][1] += int(b - a)
                else:
                    zero_runs.append([start, int(b - a)])
        segments += nseg

        if short:
            vals = data.view("<i2")
            peak = max(peak, int(vals.max()), -int(vals.min()))
        if len(keep) < keep_samples * bps:
            keep += data[:keep_samples * bps - len(keep)].tobytes()
        if loss.value:
            losses += 1
            loss_at.append(captured)
        captured += n
        first = False
    elapsed = time.monotonic() - t0
    # Rate from after the first call, which includes the purge and start-up.
    steady = time.monotonic() - t_first if t_first else 0

    result.update(
        requested_samples=total,
        captured_samples=captured,
        elapsed_s=round(elapsed, 3),
        sustained_MSps=round((captured - first_n) / steady / 1e6, 4) if steady else 0,
        sample_loss_flags=losses,
        sync_flags=sync_flags,
        output_nonzero_bytes=nonzero,
        output_nonzero_pct=round(100.0 * nonzero / max(1, captured * bps), 4),
        first_timestamp_ns=first_ns,
        timestamp_max_error_ns=None if ts_err is None else int(ts_err),
        peak_abs_lsb=peak if short else None,
        sample_head=bytes(keep[:128]).hex(" "),
        zero_map={"segment": seg, "segments": segments, "zero_runs": zero_runs[:2000],
                  "zero_runs_total": len(zero_runs), "loss_at": loss_at[:500],
                  "sync_at": sync_at[:500]},
        short=short,
    )
    result["_keep"] = bytes(keep)
    result["transport"] = transport.snapshot()
    api("smAbort", dev)
    if repair:
        repair.mode = saved_mode
    return result


def run_sweep(api, dev, transport, cfg):
    """A sweep exercises the same RF chain and ADC without the I/Q path.
    Real spectrum here with zero I/Q would localise the fault to I/Q mode."""
    result = {}
    api("smAbort", dev)
    api("smSetSweepSpeed", dev, cfg.get("speed", smSweepSpeedNormal))
    api("smSetSweepCenterSpan", dev, cfg.get("center", 1e9), cfg.get("span", 20e6))
    api("smSetSweepCoupling", dev, cfg.get("rbw", 100e3), cfg.get("rbw", 100e3), 0.001)
    api("smSetSweepDetector", dev, smDetectorMinMax, smVideoLog)
    api("smSetSweepScale", dev, smScaleLog)
    api("smSetSweepWindow", dev, smWindowFlatTop)
    api("smSetAttenuator", dev, SM_AUTO_ATTEN)
    api("smSetRefLevel", dev, cfg.get("ref_level", -20.0))

    transport.snapshot()
    status = api("smConfigure", dev, smModeSweeping)
    result["configure_status"] = status
    if status < 0:
        result["error"] = api.err(status)
        result["transport"] = transport.snapshot()
        return result

    rbw, vbw, start, binsize = (ctypes.c_double() for _ in range(4))
    size = ctypes.c_int()
    api("smGetSweepParameters", dev, ctypes.byref(rbw), ctypes.byref(vbw),
        ctypes.byref(start), ctypes.byref(binsize), ctypes.byref(size))
    n = size.value
    result.update(rbw=rbw.value, start_freq=start.value, bin_size=binsize.value,
                  sweep_size=n)
    if n <= 0:
        result["error"] = "sweep size zero"
        result["transport"] = transport.snapshot()
        return result

    lo = (ctypes.c_float * n)()
    hi = (ctypes.c_float * n)()
    ts = ctypes.c_int64()
    status = api("smGetSweep", dev, lo, hi, ctypes.byref(ts))
    result["sweep_status"] = status
    vals = list(hi)
    finite = [v for v in vals if v == v and abs(v) < 1e30]
    if finite:
        peak = max(finite)
        result.update(
            peak_dBm=round(peak, 2),
            peak_freq_Hz=start.value + binsize.value * vals.index(peak),
            median_dBm=round(sorted(finite)[len(finite) // 2], 2),
            min_dBm=round(min(finite), 2),
            all_identical=len(set(finite)) == 1,
        )
    result["nonzero_bins"] = sum(1 for v in vals if v != 0)
    result["_sweep"] = (start.value, binsize.value, vals)
    result["transport"] = transport.snapshot()
    api("smAbort", dev)
    return result


# name -> (kind, config, why it is worth running)
EXPERIMENTS = {
    "sweep": ("sweep", {"center": 1e9, "span": 20e6},
              "Does the RF chain and ADC produce real data outside I/Q mode?"),
    "sweep-fm": ("sweep", {"center": 100e6, "span": 20e6, "ref_level": -10.0},
                 "Broadcast FM should show obvious carriers if anything is connected."),
    "sweep-wifi": ("sweep", {"center": 2.442e9, "span": 80e6, "ref_level": -30.0},
                   "2.4 GHz should be busy almost anywhere."),
    "iq-dec256": ("iq", {"decimation": 256, "center": 1e9},
                  "Baseline: the configuration that produced zeros."),
    "iq-dec8-short": ("iq", {"decimation": 8, "center": 1e9, "short": True},
                      "Hardware-only decimation, 16-bit: skips the whole software "
                      "DSP chain including the stubbed FIR."),
    # The A/B that answers the filter question in one session. Same settings,
    # repair on then off. If repaired is non-zero and the raw one is zeros, the
    # impulse filters are the cause. Run these together and compare. The
    # tables run uses the constant tables instead, for comparison; it needs
    # filter_tables.json.
    "iq-ab-repair-on": ("iq", {"decimation": 8, "center": 1e9, "short": True,
                               "repair": "design"},
                        "A/B, repair ON: filters designed as the Linux build "
                        "would for these settings."),
    "iq-ab-repair-tables": ("iq", {"decimation": 8, "center": 1e9, "short": True,
                                   "repair": "tables"},
                            "A/B, repair with the constant tables extracted from "
                            "a Linux .so, same settings."),
    "iq-ab-repair-off": ("iq", {"decimation": 8, "center": 1e9, "short": True,
                                "repair": False},
                         "A/B, repair OFF: the library's own impulse filters, "
                         "same settings as iq-ab-repair-on."),
    "iq-dec8-float": ("iq", {"decimation": 8, "center": 1e9},
                      "Same path but with the 16sc to 32fc conversion in play."),
    "iq-dec16-short": ("iq", {"decimation": 16, "center": 1e9, "short": True},
                       "First decimation that engages software filtering."),
    "iq-dec1-short": ("iq", {"decimation": 1, "center": 1e9, "short": True,
                             "native_only": True},
                      "Native 200 MS/s. The Python transport cannot keep up, so "
                      "this is expected to fail here and is skipped unless asked "
                      "for; it is the native backend's job."),
    "iq-atten0": ("iq", {"decimation": 8, "center": 1e9, "short": True, "atten": 0},
                  "Fixed 0 dB attenuation instead of auto, in case auto is "
                  "parking the attenuator somewhere odd."),
    "iq-presel-off": ("iq", {"decimation": 8, "center": 1e9, "short": True,
                             "preselector": smFalse},
                      "Rules out the preselector filtering everything away."),
    "iq-fm": ("iq", {"decimation": 8, "center": 100e6, "short": True,
                     "ref_level": -10.0},
              "Strong known signal, low frequency, no preselector concerns."),
    "iq-wifi": ("iq", {"decimation": 8, "center": 2.442e9, "short": True,
                       "ref_level": -30.0},
                "Strong known signal well above the preselector crossover."),
    "iq-lte-rate": ("iq", {"decimation": 8, "center": 1e9, "short": True,
                           "base_rate": smIQStreamSampleRateLTE},
                    "Different base sample rate, different FPGA configuration."),
    "iq-retune": ("iq", {"decimation": 8, "center": 1.5e9, "short": True,
                         "queue_ms": 5.24},
                  "Exercises retune and the semaphore teardown path."),
    # The first framing slip on hardware began inside iq-atten0. Repeating it
    # shows whether that setting provokes it or it was chance.
    "iq-atten0-again": ("iq", {"decimation": 8, "center": 1e9, "short": True, "atten": 0},
                        "Repeat of iq-atten0: is the framing slip reproducible?"),
    # Ten times the usual dwell, to count how often slips happen at all. That
    # rate decides whether long decimation-1 captures can be trusted.
    "iq-soak": ("iq", {"decimation": 8, "center": 1e9, "short": True, "seconds": 5.0},
                "Five-second stream: how often do framing slips occur?"),
}


def ab_verdict(experiments):
    """Read the repair-on and repair-off A/B and state what it means, in words.

    Returns a sentence, or None if the pair was not run. Nothing here is
    inferred beyond the two results in front of it."""
    by = {e["name"]: e for e in experiments}
    on, off = by.get("iq-ab-repair-on"), by.get("iq-ab-repair-off")
    if not on or not off or "result" not in on or "result" not in off:
        return None
    ron, roff = on["result"], off["result"]

    def summary(r):
        if not r.get("captured_samples"):
            return "no data (" + str(r.get("getiq_error") or r.get("error")
                                     or "nothing captured") + ")"
        return f"{r.get('output_nonzero_pct', 0)}% non-zero"

    non_on = ron.get("captured_samples") and ron.get("output_nonzero_pct", 0) > 0.5
    non_off = roff.get("captured_samples") and roff.get("output_nonzero_pct", 0) > 0.5
    line = f"repair on: {summary(ron)}; repair off: {summary(roff)}. "
    if not ron.get("captured_samples") and not roff.get("captured_samples"):
        line += ("Neither captured data, so this says nothing about the filters; "
                 "look at the status trace for why.")
    elif non_on and not non_off:
        line += ("Repaired samples are real and the raw ones are zeros: the "
                 "impulse filters are the cause and the repair is the fix.")
    elif non_on and non_off:
        line += ("Both are non-zero, so samples do not depend on the repair; the "
                 "zeros seen elsewhere have another cause.")
    elif not non_on and non_off:
        line += "Unexpected: the repair produced zeros where the raw upload did not."
    else:
        line += ("Both are zeros with data flowing, so the impulse filters are "
                 "not the only problem; the setup path needs comparing against "
                 "the Linux build.")
    tables = by.get("iq-ab-repair-tables")
    if tables and "result" in tables:
        line += f" With the constant tables instead: {summary(tables['result'])}."
    return line


def device_state(api, dev, lib, trace):
    """Everything queryable that costs nothing and might matter.

    Several of these getters fetch the aux block from the device, and a failed
    fetch sets the library's sticky connection-lost status, so `trace` records
    the status after each call.

    No smNetworkedSpeedTest here: it keeps 32 MB of dummy data in flight at line
    rate, which the Python transport cannot drain, so it always failed and left
    the status stuck at -6 before the first experiment.
    """
    out = {}
    dtype, serial = ctypes.c_int(), ctypes.c_int()
    api("smGetDeviceInfo", dev, ctypes.byref(dtype), ctypes.byref(serial))
    out["device_type"] = DEVICE_TYPES.get(dtype.value, dtype.value)
    out["serial"] = serial.value
    maj, mnr, rev = (ctypes.c_int() for _ in range(3))
    api("smGetFirmwareVersion", dev, ctypes.byref(maj), ctypes.byref(mnr),
        ctypes.byref(rev))
    out["firmware"] = f"{maj.value}.{mnr.value}.{rev.value}"
    out["api_version"] = lib.smGetAPIVersion().decode()
    trace("smGetDeviceInfo, smGetFirmwareVersion")

    for label, name, count in (("diagnostics", "smGetDeviceDiagnostics", 3),
                               ("sfp", "smGetSFPDiagnostics", 4)):
        vals = [ctypes.c_float() for _ in range(count)]
        if api(name, dev, *[ctypes.byref(v) for v in vals]) == 0:
            out[label] = [round(v.value, 3) for v in vals]
        trace(name)

    for label, name in (("power_state", "smGetPowerState"),
                        ("reference", "smGetReference"),
                        ("gps_state", "smGetGPSState"),
                        ("attenuator", "smGetAttenuator"),
                        ("preselector", "smGetPreselector"),
                        ("current_mode", "smGetCurrentMode")):
        v = ctypes.c_int()
        if api(name, dev, ctypes.byref(v)) == 0:
            out[label] = v.value
        trace(name)

    cal = ctypes.c_uint64()
    if api("smGetCalDate", dev, ctypes.byref(cal)) == 0 and cal.value:
        out["last_cal"] = time.strftime("%Y-%m-%d", time.gmtime(cal.value))
    trace("smGetCalDate")
    return out


# ---- checks -------------------------------------------------------------------
def iq_checks(cfg, r, t):
    """Pass/fail checks for one I/Q experiment. Each is {name, ok, detail, info}.

    ok is True, False, or None when the check does not apply. info checks are
    shown but do not decide the verdict, because what they measure is not yet
    known well enough to fail on.
    """
    checks = []

    def add(name, ok, detail, info=False):
        checks.append({"name": name, "ok": ok, "detail": detail, "info": info})

    expect_zeros = cfg.get("repair") is False
    got, want = r.get("captured_samples", 0), r.get("requested_samples", 0)
    err = r.get("getiq_error") or r.get("error")
    add("data", bool(got) and got >= want and not err,
        f"{got} of {want} samples" + (f"; {err}" if err else ""))

    pct = r.get("output_nonzero_pct", 0)
    if expect_zeros:
        add("samples", got > 0 and pct <= 0.5,
            f"{pct}% non-zero; the unrepaired impulse filters should give zeros")
    else:
        add("samples", pct > 0.5, f"{pct}% non-zero")

    mode = r.get("repair_mode", "off")
    if mode != "off":
        reps = t.get("filter_repairs", [])
        done = [x for x in reps if x.get("repaired")]
        stages = sorted({x["stage"] for x in done})
        missed = len(reps) - len(done)
        cut = ", ".join(f"{x['stage']}: {x['fc']:.5f}" if "fc" in x else str(x["stage"])
                        for x in done) or "no impulse uploads seen"
        add("filters", stages == [1, 2, 3, 4] and not missed,
            f"{mode}, stage cutoffs {cut}" + (f"; {missed} not repaired" if missed else ""))

    if not expect_zeros:
        zm = r.get("zero_map") or {}
        zero = sum(n for _, n in zm.get("zero_runs", []))
        add("holes", zm.get("segments", 0) > 0 and zero == 0,
            f"{zero} of {zm.get('segments', 0)} stretches of {zm.get('segment', '?')} "
            f"samples were all zero")

    lost, timeouts = t.get("lost", 0), t.get("timeouts", 0)
    add("transport", lost == 0 and timeouts == 0,
        f"{lost} datagrams lost" + (f", {timeouts} transfers timed out" if timeouts else ""))

    add("library flags", r.get("sample_loss_flags", 0) == 0 and r.get("sync_flags", 0) == 0,
        f"{r.get('sample_loss_flags', 0)} sample-loss, {r.get('sync_flags', 0)} sync")

    rate, mps = r.get("sample_rate"), r.get("sustained_MSps")
    if rate and got:
        add("throughput", mps * 1e6 >= 0.95 * rate,
            f"read {mps} MS/s of {rate / 1e6:.4g} MS/s")

    te = r.get("timestamp_max_error_ns")
    if te is not None and rate:
        add("timestamps", te <= max(1000, 2e9 / rate),
            f"worst {te} ns from first timestamp + samples / rate", info=True)
    return checks


def sweep_checks(r, t):
    ok = r.get("all_identical") is False and r.get("nonzero_bins", 0) > 0
    return [{"name": "spectrum", "ok": ok, "info": False,
             "detail": (f"peak {r.get('peak_dBm')} dBm, median {r.get('median_dBm')} dBm"
                        if ok else r.get("error") or "flat or empty")},
            {"name": "transport", "ok": t.get("lost", 0) == 0, "info": False,
             "detail": f"{t.get('lost', 0)} datagrams lost"}]


def passed(checks):
    real = [c for c in checks if not c["info"] and c["ok"] is not None]
    return bool(real) and all(c["ok"] for c in real)


# ---- native backend ---------------------------------------------------------------
class NativeTransport:
    """The native backend behind the snapshot() and repair the harness expects.

    Native counters are cumulative, so each snapshot reports the change since
    the last. The native side keeps no command or aux logs.
    """

    def __init__(self, native, N):
        self.native, self.N = native, N
        self.repair = native.filter_repair
        self.prev = N.stats(native).as_dict()
        self.seen = self.repair.seen
        self.skip = {"filter_repairs", "commands", "first_misframe", "max_outstanding",
                     "lib_promotions"}

    def snapshot(self):
        cur = self.N.stats(self.native).as_dict()
        out = {k: cur[k] - self.prev[k] for k in self.N.STAT_U64 if k not in self.skip}
        out["calls"] = out["transfers"]
        out["commands_sent"] = cur["commands"] - self.prev["commands"]
        out["filter_repair_count"] = cur["filter_repairs"] - self.prev["filter_repairs"]
        new = min(self.repair.seen - self.seen, len(self.repair.log))
        out["filter_repairs"] = self.repair.log[-new:] if new else []
        self.seen = self.repair.seen
        for k in ("max_outstanding", "lib_promotions", *self.N.STAT_I32):
            out[k] = cur[k]
        out.update(commands=[], aux_blocks=[], events=[], raw_sample=None)
        self.prev = cur
        return out


def native_available():
    import sm_native as N
    try:
        N.native_lib_path()
        return True
    except SystemExit:
        return False


def install_backend(backend, dylib, filter_mode):
    """Load the library with one backend installed. Returns (lib, transport, slide)."""
    if backend == "native":
        import sm_native as N
        lib, native = N.install(dylib, filter_mode=filter_mode)
        bind(lib)
        anchor = T.symbol_addresses(dylib, {T.ANCHOR_SYM})[T.ANCHOR_SYM]
        slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - anchor
        return lib, NativeTransport(native, N), slide
    addrs = T.symbol_addresses(dylib, {T.VTABLE_SYM, T.ANCHOR_SYM})
    lib = ctypes.CDLL(dylib)
    bind(lib)
    slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - addrs[T.ANCHOR_SYM]
    transport = T.Transport(T.load_filter_repair(dylib, slide, filter_mode))
    T.patch_vtable(addrs[T.VTABLE_SYM] + slide, transport)
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sem_shim.dylib")
    if not os.path.exists(shim):
        sys.exit("sem_shim.dylib not found; build it first")
    T.patch_semaphores(dylib, slide, shim)
    return lib, transport, slide


# ---- one backend, in this process --------------------------------------------------
def charts_for(kind, r):
    """Render the charts for one experiment and drop the raw data they came from."""
    keep = r.pop("_keep", None)
    sweep = r.pop("_sweep", None)
    out = {}
    try:
        if kind == "iq" and r.get("captured_samples"):
            out["zero_map"] = sm_charts.zero_map_svg(r.get("zero_map"), r["captured_samples"])
            if keep:
                iq = sm_charts.to_complex(keep, r.get("short"))
                rate = r.get("sample_rate") or 1.0
                out["trace"] = sm_charts.iq_trace_svg(iq, rate)
                out["spectrogram"] = sm_charts.spectrogram(iq, rate, r.get("center_actual", 0))
        elif kind == "sweep" and sweep:
            out["sweep"] = sm_charts.sweep_svg(*sweep)
    except Exception as exc:                             # a chart must not sink the run
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def run_backend(args, names, backend):
    """Open the device on one backend, run the experiments, write <outdir>/<backend>/.

    Returns (report, charts).
    """
    outdir = os.path.join(args.outdir, backend)
    os.makedirs(outdir, exist_ok=True)
    lib, transport, slide = install_backend(backend, args.dylib, args.filter_repair)

    api = Api(lib)
    handle = ctypes.c_int(-1)
    status = api("smOpenNetworkedDevice", ctypes.byref(handle), args.host.encode(),
                 args.device.encode(), args.port)
    if status < 0:
        sys.exit(f"open failed: {status} ({api.err(status)})")
    dev = handle.value

    open_transport = transport.snapshot()
    status = T.InterfaceStatus(args.dylib, slide)
    status_trace = [{"after": "smOpenNetworkedDevice", "status": status.read(dev)}]

    def trace(call):
        status_trace.append({"after": call, "status": status.read(dev)})

    device = device_state(api, dev, lib, trace)
    state_transport = transport.snapshot()
    report = {
        "backend": backend,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": args.host, "device_addr": args.device, "port": args.port,
        "dylib": os.path.abspath(args.dylib),
        "device": device,
        "status_trace": status_trace,
        "open_phase": {
            "commands": summarise_commands(open_transport["commands"]),
            "datagrams": open_transport["datagrams"],
            "payload_bytes": open_transport["payload_bytes"],
            "events": open_transport["events"],
        },
        "state_phase": {
            "datagrams": state_transport["datagrams"],
            "events": state_transport["events"],
        },
        "experiments": [],
    }
    charts = {}

    def save():
        # After every experiment, so a crash part way keeps what finished.
        with open(os.path.join(outdir, "report.json"), "w") as f:
            json.dump(report, f, indent=2, default=str)
        with open(os.path.join(outdir, "charts.json"), "w") as f:
            json.dump(charts, f)

    stuck = next((t["after"] for t in status_trace if t["status"]), None)
    if stuck:
        print(f"\n! the connection-lost status was set during {stuck}")
    print(json.dumps(report["device"], indent=2))

    explicit = set(names) if (args.only or args.interactive) else set()
    for name in names:
        kind, cfg, why = EXPERIMENTS[name]
        # The Python transport cannot keep up with native-only experiments. In
        # a combined run the native backend covers them; alone, run them only
        # when named.
        if backend == "python" and cfg.get("native_only") and (
                args.part_of_combined or name not in explicit):
            print(f"\n--- {name} ({kind}) --- skipped (native backend only)")
            report["experiments"].append(
                {"name": name, "kind": kind, "config": dict(cfg), "why": why,
                 "skipped": "native backend only"})
            continue
        want = cfg.get("repair")
        if isinstance(want, str) and not (transport.repair and transport.repair.available(want)):
            reason = ("needs filter_tables.json" if want == "tables"
                      else f"filter repair {want} not available")
            print(f"\n--- {name} ({kind}) --- skipped ({reason})")
            report["experiments"].append(
                {"name": name, "kind": kind, "config": dict(cfg), "why": why,
                 "skipped": reason})
            continue
        print(f"\n--- {name} ({kind}) ---")
        entry = {"name": name, "kind": kind, "config": dict(cfg), "why": why}
        # Each experiment starts clean. A status left at -6 by an earlier one
        # would otherwise fail everything after it; record that it happened.
        entry["status_in"] = status.clear(dev)
        if entry["status_in"]:
            print(f"  status was {entry['status_in']} on entry; cleared")
        try:
            if kind == "iq":
                r = run_iq(api, dev, transport, cfg, args.seconds, args.keep_samples)
            else:
                r = run_sweep(api, dev, transport, cfg)
        except Exception as exc:                       # keep the run going
            import traceback
            traceback.print_exc()
            entry["error"] = f"{type(exc).__name__}: {exc}"
            r = {"transport": transport.snapshot()}
        entry["status_out"] = status.read(dev)
        t = r.pop("transport", {})
        raw = t.pop("raw_sample", None)
        if raw:
            path = os.path.join(outdir, f"{name}-raw_transfer.bin")
            open(path, "wb").write(raw)
            t["raw_transfer_file"] = os.path.basename(path)
        t["aux_decoded"] = [decode_aux(a) for a in t.get("aux_blocks", [])[:3]]
        t["commands"] = summarise_commands(t.get("commands", []))
        t.pop("aux_blocks", None)
        charts[name] = charts_for(kind, r)
        r["transport"] = t
        entry["result"] = r
        if "error" not in entry:
            entry["checks"] = iq_checks(cfg, r, t) if kind == "iq" else sweep_checks(r, t)
            entry["passed"] = passed(entry["checks"])

        if kind == "sweep":
            print(f"  peak {r.get('peak_dBm')} dBm at {r.get('peak_freq_Hz')} Hz, "
                  f"median {r.get('median_dBm')} dBm, flat={r.get('all_identical')}")
        for c in entry.get("checks", []):
            mark = "info" if c["info"] else ("ok  " if c["ok"] else "FAIL")
            print(f"  {mark} {c['name']}: {c['detail']}")
        print(f"  => {'PASS' if entry.get('passed') else 'FAIL'}  status out "
              f"{entry['status_out']}  payload {t.get('payload_bytes', 0)/1e6:.1f} MB")
        report["experiments"].append(entry)
        save()

    report["ab_verdict"] = ab_verdict(report["experiments"])
    if report["ab_verdict"]:
        print("\n=== filter A/B ===\n  " + report["ab_verdict"])
    report["filter_repair"] = transport.repair.mode if transport.repair else "off"
    report["api_errors"] = api.log
    api("smAbort", dev)
    api("smCloseDevice", dev)
    report["completed"] = True
    save()
    return report, charts


# ---- report -------------------------------------------------------------------
def write_report(outdir, results, charts, order):
    """One report.json and one self-contained report.html across backends."""
    combined = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "order": order,
                "backends": results}
    jpath = os.path.join(outdir, "report.json")
    with open(jpath, "w") as f:
        json.dump(combined, f, indent=2, default=str)

    def embed(obj):                    # safe inside a <script> element
        return json.dumps(obj, default=str).replace("</", "<\\/")
    hpath = os.path.join(outdir, "report.html")
    with open(hpath, "w") as f:
        f.write(HTML.replace("CHARTS_JSON", embed(charts))
                    .replace("REPORT_JSON", embed(combined)))
    print(f"\nwrote {jpath}\nwrote {hpath}")


HTML = r"""<!doctype html><meta charset=utf-8><title>SM200C diagnostics</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
:root{--bg:#fbfbfa;--fg:#1a1a18;--dim:#6b6b66;--line:#e0e0dc;--ok:#1a7f4b;--bad:#b3261e;--warn:#8a6100}
@media(prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#e8e8e4;--dim:#9a9a94;--line:#33333a;--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,sans-serif;max-width:1040px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;font-weight:600}
h3{font-size:14px;margin:16px 0 4px;font-weight:600}
.dim{color:var(--dim)} .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--dim)}
details{border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:6px 0}
summary{cursor:pointer;font-weight:600} pre{overflow-x:auto;font-size:12px;color:var(--dim)}
ul.checks{list-style:none;padding:0;margin:4px 0;font-size:13px} ul.checks li{padding:1px 0}
.chart{margin:6px 0 2px} .cap{font-size:12px;color:var(--dim);margin-bottom:8px}
img.spec{width:100%;height:240px;image-rendering:pixelated;display:block}
.backend{border-top:1px solid var(--line);margin-top:10px;padding-top:4px}
</style>
<h1>SM200C on macOS: diagnostics</h1>
<div class=dim id=meta></div><div id=body></div>
<script>
const R = REPORT_JSON;
const C = CHARTS_JSON;
const B = Object.keys(R.backends);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const first = B.map(b => R.backends[b]).find(r => r.device) || {device: {}};
const d = first.device || {};
document.getElementById('meta').textContent =
  `${d.device_type||'?'} serial ${d.serial||'?'} · firmware ${d.firmware||'?'} · API ${d.api_version||'?'} · ${R.started} · backends: ${B.join(', ')}`;
const byName = b => Object.fromEntries((R.backends[b].experiments || []).map(e => [e.name, e]));
const E = Object.fromEntries(B.map(b => [b, byName(b)]));
function verdict(e) {
  if (!e) return ['not run', 'dim'];
  if (e.skipped) return ['skipped: ' + e.skipped, 'dim'];
  if (e.error) return ['failed: ' + e.error, 'bad'];
  const bad = (e.checks || []).filter(c => !c.info && c.ok === false);
  if (!(e.checks || []).length) return ['ran', ''];
  return e.passed ? ['pass', 'ok'] : ['FAIL: ' + bad.map(c => c.name).join(', '), 'bad'];
}
let h = '';
for (const b of B) {
  const r = R.backends[b];
  if (r.error) h += `<div class=bad>${esc(b)} backend did not complete: ${esc(r.error)}</div>`;
}
for (const b of B) {
  const r = R.backends[b];
  if (r.ab_verdict) h += `<h2>Filter A/B, ${esc(b)} backend</h2><div>${esc(r.ab_verdict)}</div>`;
}
h += '<h2>Summary</h2><div class=wrap><table><tr><th>experiment</th>'
  + B.map(b => `<th>${esc(b)}</th><th>rate</th>`).join('') + '</tr>';
for (const name of R.order) {
  h += `<tr><td><a href="#x-${esc(name)}">${esc(name)}</a></td>`;
  for (const b of B) {
    const e = E[b][name], [v, cls] = verdict(e), r = (e && e.result) || {};
    const rate = r.sustained_MSps ? r.sustained_MSps + ' MS/s'
      : (r.peak_dBm !== undefined ? r.peak_dBm + ' dBm peak' : '&mdash;');
    h += `<td class=${cls}>${esc(v)}</td><td>${rate}</td>`;
  }
  h += '</tr>';
}
h += '</table></div>';
h += '<h2>Device state</h2><div class=wrap><table><tr><th>field</th><th>value</th></tr>';
for (const [k, v] of Object.entries(d)) h += `<tr><td>${esc(k)}</td><td>${esc(JSON.stringify(v))}</td></tr>`;
h += '</table></div>';
for (const b of B) {
  const r = R.backends[b];
  h += `<details><summary>Setup, ${esc(b)} backend (filter repair: ${esc(r.filter_repair || '?')})</summary>`
    + '<div class=dim>-6 means a transfer or command failed. The library never clears it on its own; '
    + 'each experiment starts with it cleared.</div><div class=wrap><table><tr><th>after</th><th>status</th></tr>';
  for (const t of (r.status_trace || [])) h += `<tr><td>${esc(t.after)}</td><td class=${t.status ? 'bad' : 'ok'}>${esc(t.status)}</td></tr>`;
  h += '</table></div>';
  const ev = [...((r.open_phase||{}).events||[]), ...((r.state_phase||{}).events||[])];
  if (ev.length) h += '<div class=bad>Transport failures during setup: ' + esc(JSON.stringify(ev)) + '</div>';
  h += '</details>';
}
h += '<h2>Experiments</h2>';
for (const name of R.order) {
  const any = B.map(b => E[b][name]).find(e => e) || {};
  const failing = B.some(b => verdict(E[b][name])[1] === 'bad');
  h += `<details id="x-${esc(name)}" ${failing ? 'open' : ''}><summary>${esc(name)} `
    + B.map(b => { const [v, cls] = verdict(E[b][name]); return `<span class=${cls}>· ${esc(b)}: ${esc(v)}</span>`; }).join(' ')
    + `</summary><div class=dim>${esc(any.why || '')}</div>`;
  for (const b of B) {
    const e = E[b][name];
    const [v, cls] = verdict(e);
    h += `<div class=backend><h3>${esc(b)} backend: <span class=${cls}>${esc(v)}</span></h3>`;
    if (!e || e.skipped) { h += '</div>'; continue; }
    if (e.checks) {
      h += '<ul class=checks>';
      for (const c of e.checks) {
        const m = c.info ? ['info', 'dim'] : (c.ok ? ['pass', 'ok'] : ['FAIL', 'bad']);
        h += `<li><span class=${m[1]}>${m[0]}</span> <b>${esc(c.name)}</b>: ${esc(c.detail)}</li>`;
      }
      h += '</ul>';
    }
    const ch = (C[b] || {})[name] || {};
    if (ch.error) h += `<div class=warn>chart error: ${esc(ch.error)}</div>`;
    if (ch.zero_map) h += `<div class=chart>${ch.zero_map}</div><div class=cap>Zero map across the whole capture: `
      + 'green has data, red stretches came back as exact zeros, amber ticks are sample-loss flags, purple ticks sync errors.</div>';
    if (ch.trace) h += `<div class=chart>${ch.trace}</div>`;
    if (ch.spectrogram && ch.spectrogram.png) h += `<img class=spec alt="spectrogram" src="${ch.spectrogram.png}">`
      + `<div class=cap>Spectrogram of the kept samples: ${esc(ch.spectrogram.caption)}</div>`;
    if (ch.sweep) h += `<div class=chart>${ch.sweep}</div>`;
    h += `<details><summary class=dim>raw result</summary><pre>${esc(JSON.stringify(e, null, 2))}</pre></details></div>`;
  }
  h += '</details>';
}
document.getElementById('body').innerHTML = h;
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dylib", nargs="?")
    ap.add_argument("--host", default="192.168.2.2")
    ap.add_argument("--device", default="192.168.2.10")
    ap.add_argument("--port", type=int, default=51665)
    ap.add_argument("--all", action="store_true", help="run every experiment")
    ap.add_argument("--only", help="comma-separated experiment names")
    ap.add_argument("--list", action="store_true", help="show experiments and exit")
    ap.add_argument("--interactive", action="store_true", help="pick from a menu")
    ap.add_argument("--seconds", type=float, default=0.5, help="dwell per I/Q test")
    ap.add_argument("--keep-samples", type=int, default=65536,
                    help="samples kept per experiment for the charts, 0 to skip")
    ap.add_argument("--outdir", default="sm_diag_out")
    ap.add_argument("--backends", default="python,native",
                    help="comma-separated: python, native, or both (default both, "
                         "each in its own process, one combined report)")
    ap.add_argument("--filter-repair", choices=("design", "tables", "off"),
                    default="design",
                    help="what replaces the impulse filter uploads (default design)")
    ap.add_argument("--no-filter-repair", action="store_true",
                    help="same as --filter-repair off")
    ap.add_argument("--part-of-combined", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.no_filter_repair:
        args.filter_repair = "off"

    if args.list:
        for name, (kind, cfg, why) in EXPERIMENTS.items():
            tag = " [native only]" if cfg.get("native_only") else ""
            print(f"{name:20} [{kind}]{tag} {why}")
        return
    if not args.dylib:
        ap.error("dylib is required unless using --list")

    names = list(EXPERIMENTS)
    if args.only:
        names = [n.strip() for n in args.only.split(",")]
        unknown = [n for n in names if n not in EXPERIMENTS]
        if unknown:
            sys.exit(f"unknown experiment(s): {unknown}")
    elif args.interactive:
        for i, (name, (kind, _, why)) in enumerate(EXPERIMENTS.items(), 1):
            print(f"{i:2}. {name:20} {why}")
        pick = input("\nnumbers, comma separated, or blank for all: ").strip()
        if pick:
            idx = [int(p) for p in pick.split(",")]
            names = [list(EXPERIMENTS)[i - 1] for i in idx]
    elif not args.all:
        sys.exit("pick --all, --only NAMES, --interactive or --list")

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    if any(b not in ("python", "native") for b in backends):
        sys.exit("--backends takes python, native or both")
    if "native" in backends and not native_available():
        print("libsmnative.dylib not found (make -C native); running the Python backend only")
        backends = [b for b in backends if b != "native"] or ["python"]
    os.makedirs(args.outdir, exist_ok=True)

    if len(backends) == 1:
        report, charts = run_backend(args, names, backends[0])
        if not args.part_of_combined:
            write_report(args.outdir, {backends[0]: report}, {backends[0]: charts}, names)
        return

    # Both backends patch the same vtable slots and semaphore imports, so each
    # gets a process of its own, one after the other, then one combined report.
    results, charts = {}, {}
    for i, b in enumerate(backends):
        if i:
            time.sleep(1.0)                       # let the device settle after close
        print(f"\n========== {b} backend ==========")
        cmd = [sys.executable, os.path.abspath(__file__), args.dylib,
               "--host", args.host, "--device", args.device, "--port", str(args.port),
               "--only", ",".join(names), "--seconds", str(args.seconds),
               "--keep-samples", str(args.keep_samples), "--outdir", args.outdir,
               "--filter-repair", args.filter_repair, "--backends", b,
               "--part-of-combined"]
        sub = os.path.join(args.outdir, b)
        for stale in ("report.json", "charts.json"):
            if os.path.exists(os.path.join(sub, stale)):
                os.remove(os.path.join(sub, stale))
        rc = subprocess.call(cmd)
        try:
            with open(os.path.join(sub, "report.json")) as f:
                results[b] = json.load(f)
            with open(os.path.join(sub, "charts.json")) as f:
                charts[b] = json.load(f)
        except (OSError, ValueError):
            results[b] = {"backend": b, "experiments": []}
            charts[b] = {}
        if rc or not results[b].get("completed"):
            done = len(results[b]["experiments"])
            results[b]["error"] = (f"exited with status {rc} after {done} experiment"
                                   f"{'' if done == 1 else 's'}")
            print(f"! {b} backend: {results[b]['error']}")
    write_report(args.outdir, results, charts, names)

    print("\n========== summary ==========")
    for name in names:
        cells = []
        for b in backends:
            e = next((x for x in results[b].get("experiments", []) if x["name"] == name), None)
            v = ("not run" if e is None else "skipped" if e.get("skipped")
                 else "failed" if e.get("error") else "pass" if e.get("passed") else "FAIL")
            cells.append(f"{b} {v}")
        print(f"  {name:20} " + "   ".join(cells))


if __name__ == "__main__":
    main()
