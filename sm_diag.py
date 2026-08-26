#!/usr/bin/env python3
"""SM200C on macOS: diagnostic harness.

Opens the device once, runs a battery of experiments, and writes a single
report.json plus a self-contained report.html. The point is to answer in one
bench session why the device streams zero samples.

    python3 sm_diag.py ./libsm_api.2.3.7.dylib --list
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --all
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --only sweep,iq-dec8-short
    python3 sm_diag.py ./libsm_api.2.3.7.dylib --interactive

Every experiment is isolated: a failure is recorded and the run carries on.
"""

import argparse
import ctypes
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
def run_iq(api, dev, transport, cfg, seconds, keep_samples):
    """Configure I/Q streaming, pull samples, report what actually arrived."""
    short = cfg.get("short", False)
    bps = 4 if short else 8
    result = {}

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
        return result

    rate, bw, actual = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    api("smGetIQParameters", dev, ctypes.byref(rate), ctypes.byref(bw))
    api("smGetIQCenterFreq", dev, ctypes.byref(actual))
    scale = ctypes.c_float()
    api("smGetIQCorrection", dev, ctypes.byref(scale))
    result.update(sample_rate=rate.value, bandwidth=bw.value,
                  center_actual=actual.value, correction=scale.value)

    block = 32768
    total = max(block, int(rate.value * seconds))
    buf = ctypes.create_string_buffer(block * bps)
    ns, loss, remaining = ctypes.c_int64(), ctypes.c_int(), ctypes.c_int()

    captured, losses, nonzero, first_ns = 0, 0, 0, None
    peak = 0.0
    keep = bytearray()
    t0 = time.monotonic()
    first = True
    while captured < total:
        n = min(block, total - captured)
        status = api("smGetIQ", dev, buf, n, None, 0, ctypes.byref(ns),
                     smTrue if first else smFalse, ctypes.byref(loss),
                     ctypes.byref(remaining))
        if status < 0:
            result["getiq_error"] = api.err(status)
            break
        if first:
            first_ns = ns.value
        raw = buf.raw[:n * bps]
        nonzero += len(raw) - raw.count(0)
        if short:
            vals = struct.unpack(f"<{n * 2}h", raw)
            peak = max(peak, max(abs(v) for v in vals) if vals else 0)
        if losses == 0 and len(keep) < keep_samples * bps:
            keep += raw[:keep_samples * bps - len(keep)]
        if loss.value:
            losses += 1
        captured += n
        first = False
    elapsed = time.monotonic() - t0

    result.update(
        captured_samples=captured,
        elapsed_s=round(elapsed, 3),
        sustained_MSps=round(captured / elapsed / 1e6, 4) if elapsed else 0,
        sample_loss_flags=losses,
        output_nonzero_bytes=nonzero,
        output_nonzero_pct=round(100.0 * nonzero / max(1, captured * bps), 4),
        first_timestamp_ns=first_ns,
        peak_abs_lsb=peak if short else None,
        sample_head=keep[:128].hex(" "),
    )
    result["transport"] = transport.snapshot()
    api("smAbort", dev)
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
    "iq-dec8-float": ("iq", {"decimation": 8, "center": 1e9},
                      "Same path but with the 16sc to 32fc conversion in play."),
    "iq-dec16-short": ("iq", {"decimation": 16, "center": 1e9, "short": True},
                       "First decimation that engages software filtering."),
    "iq-dec1-short": ("iq", {"decimation": 1, "center": 1e9, "short": True},
                      "Native 200 MS/s. Expect loss; we care whether bytes are "
                      "non-zero, not whether it keeps up."),
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
}


def device_state(api, dev, lib):
    """Everything queryable that costs nothing and might matter."""
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

    for label, name, count in (("diagnostics", "smGetDeviceDiagnostics", 3),
                               ("sfp", "smGetSFPDiagnostics", 4)):
        vals = [ctypes.c_float() for _ in range(count)]
        if api(name, dev, *[ctypes.byref(v) for v in vals]) == 0:
            out[label] = [round(v.value, 3) for v in vals]

    for label, name in (("power_state", "smGetPowerState"),
                        ("reference", "smGetReference"),
                        ("gps_state", "smGetGPSState"),
                        ("attenuator", "smGetAttenuator"),
                        ("preselector", "smGetPreselector"),
                        ("current_mode", "smGetCurrentMode")):
        v = ctypes.c_int()
        if api(name, dev, ctypes.byref(v)) == 0:
            out[label] = v.value

    cal = ctypes.c_uint64()
    if api("smGetCalDate", dev, ctypes.byref(cal)) == 0 and cal.value:
        out["last_cal"] = time.strftime("%Y-%m-%d", time.gmtime(cal.value))

    # 10GbE link throughput, independent of any measurement mode
    bps = ctypes.c_double()
    if api("smNetworkedSpeedTest", dev, 1.0, ctypes.byref(bps)) == 0:
        out["link_MBps"] = round(bps.value / 1e6, 1)
    return out


HTML = """<!doctype html><meta charset=utf-8><title>SM200C diagnostics</title>
<style>
:root{--bg:#fbfbfa;--fg:#1a1a18;--dim:#6b6b66;--line:#e0e0dc;--ok:#1a7f4b;--bad:#b3261e;--warn:#8a6100}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#18181a;--fg:#e8e8e4;--dim:#9a9a94;--line:#33333a}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,sans-serif;max-width:1000px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;font-weight:600}
.dim{color:var(--dim)} .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;display:block;overflow-x:auto}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--dim)}
details{border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:6px 0}
summary{cursor:pointer;font-weight:600} pre{overflow-x:auto;font-size:12px;color:var(--dim)}
</style>
<h1>SM200C on macOS &mdash; diagnostics</h1>
<div class=dim id=meta></div><div id=body></div>
<script>
const R = REPORT_JSON;
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('meta').textContent =
  `${R.device.device_type||'?'} serial ${R.device.serial||'?'} · firmware ${R.device.firmware||'?'} · API ${R.device.api_version||'?'} · ${R.started}`;
let h = '<h2>Device state</h2><table><tr><th>field</th><th>value</th></tr>';
for (const [k,v] of Object.entries(R.device)) h += `<tr><td>${esc(k)}</td><td>${esc(JSON.stringify(v))}</td></tr>`;
h += '</table><h2>Experiments</h2><table><tr><th>name</th><th>verdict</th><th>rate</th><th>output non-zero</th><th>payload</th><th>loss</th></tr>';
for (const e of R.experiments) {
  const r = e.result || {};
  let verdict = 'ran', cls = '';
  if (e.error || r.error) { verdict = 'failed'; cls = 'bad'; }
  else if (e.kind === 'iq') {
    if (r.output_nonzero_pct > 0.5) { verdict = 'real samples'; cls = 'ok'; }
    else { verdict = 'zeros'; cls = 'bad'; }
  } else if (e.kind === 'sweep') {
    if (r.all_identical === false && r.nonzero_bins > 0) { verdict = 'real spectrum'; cls = 'ok'; }
    else { verdict = 'flat/empty'; cls = 'bad'; }
  }
  const t = r.transport || {};
  h += `<tr><td>${esc(e.name)}</td><td class=${cls}>${verdict}</td>`
    + `<td>${r.sustained_MSps ? r.sustained_MSps+' MS/s' : (r.peak_dBm!==undefined ? r.peak_dBm+' dBm peak' : '&mdash;')}</td>`
    + `<td>${r.output_nonzero_pct!==undefined ? r.output_nonzero_pct+'%' : '&mdash;'}</td>`
    + `<td>${t.payload_bytes ? (t.payload_bytes/1e6).toFixed(1)+' MB' : '&mdash;'}</td>`
    + `<td>${r.sample_loss_flags!==undefined ? r.sample_loss_flags : '&mdash;'}</td></tr>`;
}
h += '</table>';
for (const e of R.experiments) {
  h += `<details><summary>${esc(e.name)}</summary><div class=dim>${esc(e.why||'')}</div>`
    + `<pre>${esc(JSON.stringify(e, null, 2))}</pre></details>`;
}
document.getElementById('body').innerHTML = h;
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dylib")
    ap.add_argument("--host", default="192.168.2.2")
    ap.add_argument("--device", default="192.168.2.10")
    ap.add_argument("--port", type=int, default=51665)
    ap.add_argument("--all", action="store_true", help="run every experiment")
    ap.add_argument("--only", help="comma-separated experiment names")
    ap.add_argument("--list", action="store_true", help="show experiments and exit")
    ap.add_argument("--interactive", action="store_true", help="pick from a menu")
    ap.add_argument("--seconds", type=float, default=0.5, help="dwell per I/Q test")
    ap.add_argument("--keep-samples", type=int, default=65536,
                    help="samples to save per experiment, 0 to skip")
    ap.add_argument("--outdir", default="sm_diag_out")
    args = ap.parse_args()

    if args.list:
        for name, (kind, cfg, why) in EXPERIMENTS.items():
            print(f"{name:18} [{kind}] {why}")
        return

    names = list(EXPERIMENTS)
    if args.only:
        names = [n.strip() for n in args.only.split(",")]
        unknown = [n for n in names if n not in EXPERIMENTS]
        if unknown:
            sys.exit(f"unknown experiment(s): {unknown}")
    elif args.interactive:
        for i, (name, (kind, _, why)) in enumerate(EXPERIMENTS.items(), 1):
            print(f"{i:2}. {name:18} {why}")
        pick = input("\nnumbers, comma separated, or blank for all: ").strip()
        if pick:
            idx = [int(p) for p in pick.split(",")]
            names = [list(EXPERIMENTS)[i - 1] for i in idx]
    elif not args.all:
        sys.exit("pick --all, --only NAMES, --interactive or --list")

    os.makedirs(args.outdir, exist_ok=True)

    addrs = T.symbol_addresses(args.dylib, {T.VTABLE_SYM, T.ANCHOR_SYM})
    lib = ctypes.CDLL(args.dylib)
    bind(lib)
    slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - addrs[T.ANCHOR_SYM]
    transport = T.Transport()
    T.patch_vtable(addrs[T.VTABLE_SYM] + slide, transport)
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sem_shim.dylib")
    if not os.path.exists(shim):
        sys.exit("sem_shim.dylib not found; build it first")
    T.patch_semaphores(args.dylib, slide, shim)

    api = Api(lib)
    handle = ctypes.c_int(-1)
    status = api("smOpenNetworkedDevice", ctypes.byref(handle), args.host.encode(),
                 args.device.encode(), args.port)
    if status < 0:
        sys.exit(f"open failed: {status} ({api.err(status)})")
    dev = handle.value

    open_transport = transport.snapshot()
    report = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": args.host, "device_addr": args.device, "port": args.port,
        "dylib": os.path.abspath(args.dylib),
        "device": device_state(api, dev, lib),
        "open_phase": {
            "commands": summarise_commands(open_transport["commands"]),
            "datagrams": open_transport["datagrams"],
            "payload_bytes": open_transport["payload_bytes"],
        },
        "experiments": [],
    }
    print(json.dumps(report["device"], indent=2))

    for name in names:
        kind, cfg, why = EXPERIMENTS[name]
        print(f"\n--- {name} ({kind}) ---")
        entry = {"name": name, "kind": kind, "config": dict(cfg), "why": why}
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
        t = r.pop("transport", {})
        raw = t.pop("raw_sample", None)
        if raw:
            path = os.path.join(args.outdir, f"{name}-raw_transfer.bin")
            open(path, "wb").write(raw)
            t["raw_transfer_file"] = os.path.basename(path)
        t["aux_decoded"] = [decode_aux(a) for a in t.get("aux_blocks", [])[:3]]
        t["commands"] = summarise_commands(t.get("commands", []))
        t.pop("aux_blocks", None)
        r["transport"] = t
        entry["result"] = r

        head = r.get("sample_head", "")
        verdict = ("real samples" if r.get("output_nonzero_pct", 0) > 0.5
                   else "ZEROS" if kind == "iq" else "")
        print(f"  {verdict}  non-zero {r.get('output_nonzero_pct', '-')}%  "
              f"payload {t.get('payload_bytes', 0)/1e6:.1f} MB  "
              f"loss {r.get('sample_loss_flags', '-')}")
        if kind == "sweep":
            print(f"  peak {r.get('peak_dBm')} dBm at {r.get('peak_freq_Hz')} Hz, "
                  f"median {r.get('median_dBm')} dBm, flat={r.get('all_identical')}")
        if head:
            print(f"  head {head[:72]}")
        report["experiments"].append(entry)

    report["api_errors"] = api.log
    api("smAbort", dev)
    api("smCloseDevice", dev)

    jpath = os.path.join(args.outdir, "report.json")
    with open(jpath, "w") as f:
        json.dump(report, f, indent=2, default=str)
    hpath = os.path.join(args.outdir, "report.html")
    with open(hpath, "w") as f:
        f.write(HTML.replace("REPORT_JSON", json.dumps(report, default=str)))
    print(f"\nwrote {jpath}\nwrote {hpath}")


if __name__ == "__main__":
    main()
