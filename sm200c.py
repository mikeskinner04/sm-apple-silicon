"""A clean Python interface to the Signal Hound SM200C on macOS.

Wraps Signal Hound's SM API (libsm_api.dylib) with the transport, semaphore and
filter repairs from this repository already in place, so a caller only sees
the instrument:

    from sm200c import SM200C

    with SM200C("./libsm_api.2.3.7.dylib") as sm:
        print(sm.info)

        sm.ref_level = -20
        info = sm.configure_iq(center=1e9, decimation=8)
        block = sm.read_iq(1 << 20, purge=True)       # block.samples: complex64

        sm.configure_sweep(center=2.442e9, span=80e6, rbw=30e3)
        trace = sm.sweep()                            # trace.freqs, trace.max

Everything the API offers for tuning, sweeps and the four I/Q modes is here:

    front end     ref_level, attenuator, preselector, reference, power_state,
                  reference_out, gps_timebase_update, gps_state, gps_holdover
    sweeps        configure_sweep, sweep, sweeps (queued)
    I/Q stream    configure_iq, read_iq, stream_iq
    I/Q sweep     configure_iq_sweep_list, iq_sweep_list, iq_sweep_lists (queued)
    segmented     configure_segmented, capture_segmented, start_segmented,
                  collect_segmented
    full band     configure_full_band, full_band, full_band_sweep
    other         info, diagnostics, full_diagnostics, sfp_diagnostics,
                  calibration_date, mode, abort, preset, lte_resample

Only I/Q streaming and sweeps have been exercised on this transport. The I/Q
sweep list, segmented and full-band modes are wrapped faithfully from sm_api.h
but untested here; segmented capture is documented as an SM200B/SM435B
feature and may refuse on an SM200C.

Errors from the API raise SmError. Warnings (a setting clamped, ADC overload
and so on) are issued as SmWarning, except on the per-read calls, whose result
objects carry the status instead so a stream is not flooded.

The native backend is the default and is required: build it with
`make -C native`. backend="python" exists for side-by-side diagnostics only. A
process can load the library with one backend only.
"""

import ctypes
import datetime
import os
import sys
import warnings
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sm_transport as T  # noqa: E402

DEFAULT_HOST = "192.168.2.2"
DEFAULT_DEVICE = "192.168.2.10"
DEFAULT_PORT = 51665

# ---- sm_api.h enumerations, by name ------------------------------------------
DATA_TYPES = {"complex64": 0, "int16": 1}
SPEEDS = {"auto": 0, "normal": 1, "fast": 2}
DETECTORS = {"average": 0, "minmax": 1}
SCALES = {"dBm": 0, "mV": 1, "full-scale": 2}
VIDEO_UNITS = {"log": 0, "voltage": 1, "power": 2, "sample": 3}
WINDOWS = {"flattop": 0, "nutall": 2, "blackman": 3, "hamming": 4,
           "gaussian6dB": 5, "rect": 6}
BASE_RATES = {"native": 0, "lte": 1}
TRIGGERS = {"immediate": 0, "video": 1, "external": 2, "fmt": 3}
EDGES = {"rising": 0, "falling": 1}
POWER_STATES = {"on": 0, "standby": 1}
REFERENCES = {"internal": 0, "external": 1}
GPS_STATES = {0: "not present", 1: "locked", 2: "disciplined"}
MODES = {0: "idle", 1: "sweeping", 2: "real-time", 3: "iq-streaming", 4: "audio",
         5: "iq-segmented", 6: "iq-sweep-list"}
DEVICE_TYPES = {0: "SM200A", 1: "SM200B", 2: "SM200C", 3: "SM435B", 4: "SM435C"}

SM_AUTO_ATTEN = -1
SYNC_ERR = -11                 # smSyncErr: GetIQ delivered samples but flagged framing
FULL_BAND_STEP_HZ = 39.0625e6  # full-band centre = (index + 1) * this
SWEEP_QUEUE = 16               # queue positions for queued sweeps and I/Q sweeps
TRANSFER_MS = 2.62144          # one I/Q request; the queue holds 2 to 16


class SmError(RuntimeError):
    """An SM API call returned an error status."""

    def __init__(self, call, status, message):
        super().__init__(f"{call}: {message} ({status})")
        self.call, self.status, self.message = call, status, message


class SmWarning(UserWarning):
    """An SM API call succeeded with a warning status."""


def _choose(table, value, what):
    if value not in table:
        raise ValueError(f"{what} must be one of {sorted(table)}, not {value!r}")
    return table[value]


# ---- result types ---------------------------------------------------------------
@dataclass
class DeviceInfo:
    model: str
    serial: int
    firmware: str
    api_version: str


@dataclass
class SweepInfo:
    """What the device will actually sweep, after configure_sweep."""
    rbw: float
    vbw: float
    start: float
    bin_size: float
    size: int

    @property
    def freqs(self):
        return self.start + self.bin_size * np.arange(self.size)

    @property
    def stop(self):
        return self.start + self.bin_size * (self.size - 1)


@dataclass
class Sweep:
    freqs: np.ndarray
    min: np.ndarray            # per-bin minimum (min/max detector) or average
    max: np.ndarray            # per-bin maximum (min/max detector) or average
    timestamp_ns: int          # end of the sweep
    status: int = 0


@dataclass
class IQInfo:
    """What the device is actually streaming, after configure_iq."""
    center: float
    sample_rate: float
    bandwidth: float
    correction: float          # multiply int16 samples by this for corrected amplitude
    decimation: int
    data_type: str


@dataclass
class IQBlock:
    """One read of streamed I/Q.

    samples is complex64 for the "complex64" data type, or an int16 array of
    shape (n, 2) holding I and Q for "int16"; complex() converts either to
    corrected complex64.
    """
    samples: np.ndarray
    timestamp_ns: int          # time of the first sample
    sample_loss: bool          # the API's circular buffer overflowed before this read
    remaining: int             # samples still buffered in the API after this read
    correction: float
    status: int = 0            # positive values are API warnings, e.g. ADC overload
    sync_error: bool = False   # samples delivered, but an aux block was misplaced
    triggers: Optional[np.ndarray] = None

    def complex(self):
        if self.samples.dtype == np.complex64:
            return self.samples
        s = self.samples.astype(np.float32) * self.correction
        return (s[:, 0] + 1j * s[:, 1]).astype(np.complex64)


@dataclass
class SweepListStep:
    """One step of an I/Q sweep list. Set atten (0-6, 5 dB steps) to fix the
    attenuator; otherwise ref_level sets it automatically."""
    freq: float
    samples: int
    ref_level: float = -20.0
    atten: Optional[int] = None


@dataclass
class IQSweep:
    freqs: List[float]
    samples: List[np.ndarray]  # one array per step, in the configured data type
    timestamps_ns: List[int]   # time of each step's first sample
    corrections: List[float]


@dataclass
class Segment:
    """One segment of a segmented capture. For an immediate trigger the API
    adds pre_trigger to capture_size and sets pre_trigger to zero."""
    capture_size: int
    trigger: str = "immediate"
    pre_trigger: int = 0
    timeout: float = 1.0

    @property
    def length(self):
        return self.pre_trigger + self.capture_size


@dataclass
class SegmentedInfo:
    center: float
    sample_rate: float
    bandwidth: float
    correction: float
    max_captures: int
    data_type: str


@dataclass
class SegmentData:
    samples: np.ndarray
    timestamp_ns: int
    timed_out: bool


@dataclass
class FullBandCapture:
    index: int
    center: float              # (index + 1) * 39.0625 MHz
    samples: np.ndarray        # complex64, full scale


# ---- loading the library with a backend ----------------------------------------
_PROTOTYPES = None


def _prototypes():
    ci, cd, cf, u8, u32, u64, i64 = (ctypes.c_int, ctypes.c_double, ctypes.c_float,
                                     ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint64,
                                     ctypes.c_int64)
    p, vp, cc = ctypes.POINTER, ctypes.c_void_p, ctypes.c_char_p
    return {
        "smOpenNetworkedDevice": [p(ci), cc, cc, ctypes.c_uint16],
        "smCloseDevice": [ci], "smPreset": [ci], "smAbort": [ci],
        "smGetDeviceInfo": [ci, p(ci), p(ci)],
        "smGetFirmwareVersion": [ci, p(ci), p(ci), p(ci)],
        "smGetDeviceDiagnostics": [ci, p(cf), p(cf), p(cf)],
        "smGetFullDeviceDiagnostics": [ci, vp],
        "smGetSFPDiagnostics": [ci, p(cf), p(cf), p(cf), p(cf)],
        "smSetPowerState": [ci, ci], "smGetPowerState": [ci, p(ci)],
        "smSetAttenuator": [ci, ci], "smGetAttenuator": [ci, p(ci)],
        "smSetRefLevel": [ci, cd], "smGetRefLevel": [ci, p(cd)],
        "smSetPreselector": [ci, ci], "smGetPreselector": [ci, p(ci)],
        "smSetExternalReference": [ci, ci], "smGetExternalReference": [ci, p(ci)],
        "smSetReference": [ci, ci], "smGetReference": [ci, p(ci)],
        "smSetGPSTimebaseUpdate": [ci, ci], "smGetGPSTimebaseUpdate": [ci, p(ci)],
        "smGetGPSHoldoverInfo": [ci, p(ci), p(u64)],
        "smGetGPSState": [ci, p(ci)], "smGetCalDate": [ci, p(u64)],
        "smConfigure": [ci, ci], "smGetCurrentMode": [ci, p(ci)],
        # sweeps
        "smSetSweepSpeed": [ci, ci],
        "smSetSweepCenterSpan": [ci, cd, cd], "smSetSweepStartStop": [ci, cd, cd],
        "smSetSweepCoupling": [ci, cd, cd, cd],
        "smSetSweepDetector": [ci, ci, ci], "smSetSweepScale": [ci, ci],
        "smSetSweepWindow": [ci, ci], "smSetSweepSpurReject": [ci, ci],
        "smGetSweepParameters": [ci, p(cd), p(cd), p(cd), p(cd), p(ci)],
        "smGetSweep": [ci, vp, vp, p(i64)],
        "smStartSweep": [ci, ci], "smFinishSweep": [ci, ci, vp, vp, p(i64)],
        # I/Q streaming
        "smSetIQBaseSampleRate": [ci, ci], "smSetIQDataType": [ci, ci],
        "smSetIQCenterFreq": [ci, cd], "smGetIQCenterFreq": [ci, p(cd)],
        "smSetIQSampleRate": [ci, ci], "smSetIQBandwidth": [ci, ci, cd],
        "smSetIQExtTriggerEdge": [ci, ci], "smSetIQTriggerSentinel": [cd],
        "smSetIQQueueSize": [ci, cf],
        "smGetIQParameters": [ci, p(cd), p(cd)], "smGetIQCorrection": [ci, p(cf)],
        "smGetIQ": [ci, vp, ci, vp, ci, p(i64), ci, p(ci), p(ci)],
        # I/Q sweep list
        "smSetIQSweepListDataType": [ci, ci], "smSetIQSweepListCorrected": [ci, ci],
        "smSetIQSweepListSteps": [ci, ci], "smGetIQSweepListSteps": [ci, p(ci)],
        "smSetIQSweepListFreq": [ci, ci, cd], "smSetIQSweepListRef": [ci, ci, cd],
        "smSetIQSweepListAtten": [ci, ci, ci],
        "smSetIQSweepListSampleCount": [ci, ci, u32],
        "smIQSweepListGetCorrections": [ci, vp],
        "smIQSweepListGetSweep": [ci, vp, vp],
        "smIQSweepListStartSweep": [ci, ci, vp, vp],
        "smIQSweepListFinishSweep": [ci, ci],
        # segmented I/Q
        "smSetSegIQDataType": [ci, ci], "smSetSegIQCenterFreq": [ci, cd],
        "smSetSegIQVideoTrigger": [ci, cd, ci], "smSetSegIQExtTrigger": [ci, ci],
        "smSetSegIQFMTParams": [ci, ci, vp, vp, ci],
        "smSetSegIQSegmentCount": [ci, ci],
        "smSetSegIQSegment": [ci, ci, ci, ci, ci, cd],
        "smSegIQGetMaxCaptures": [ci, p(ci)],
        "smSegIQCaptureStart": [ci, ci], "smSegIQCaptureWait": [ci, ci],
        "smSegIQCaptureWaitAsync": [ci, ci, p(ci)],
        "smSegIQCaptureTimeout": [ci, ci, ci, p(ci)],
        "smSegIQCaptureTime": [ci, ci, ci, p(i64)],
        "smSegIQCaptureRead": [ci, ci, ci, vp, ci, ci],
        "smSegIQCaptureFinish": [ci, ci],
        "smSegIQLTEResample": [vp, ci, vp, p(ci), ctypes.c_bool],
        # full band
        "smSetIQFullBandAtten": [ci, ci], "smSetIQFullBandCorrected": [ci, ci],
        "smSetIQFullBandSamples": [ci, ci], "smSetIQFullBandTriggerType": [ci, ci],
        "smSetIQFullBandVideoTrigger": [ci, cd],
        "smSetIQFullBandTriggerTimeout": [ci, cd],
        "smGetIQFullBand": [ci, vp, ci], "smGetIQFullBandSweep": [ci, vp, ci, ci, ci],
        "smSetSweepGPIO": [ci, ci, u8],
    }


def bind(lib):
    """Set every prototype this module uses, straight from sm_api.h."""
    global _PROTOTYPES
    if _PROTOTYPES is None:
        _PROTOTYPES = _prototypes()
    lib.smGetErrorString.restype = ctypes.c_char_p
    lib.smGetErrorString.argtypes = [ctypes.c_int]
    lib.smGetAPIVersion.restype = ctypes.c_char_p
    for name, argtypes in _PROTOTYPES.items():
        getattr(lib, name).argtypes = argtypes


class _NativeStats:
    """Counters from the native backend, in the shape the Python Transport gives.

    Native counters are cumulative, so snapshot() reports the change since the
    last call. The native side keeps no command or aux logs.
    """
    SKIP = {"filter_repairs", "commands", "first_misframe", "max_outstanding",
            "lib_promotions"}

    def __init__(self, native, N):
        self.native, self.N = native, N
        self.repair = native.filter_repair
        self.prev = N.stats(native).as_dict()
        self.seen = self.repair.seen

    def counters(self):
        return self.N.stats(self.native).as_dict()

    def snapshot(self):
        cur = self.counters()
        out = {k: cur[k] - self.prev[k] for k in self.N.STAT_U64 if k not in self.SKIP}
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


class _PythonStats:
    """The Python Transport, with the same counters() and snapshot() as native."""
    KEYS = ("datagrams", "payload_bytes", "lost", "gaps", "resets", "duplicates",
            "stale_flushed", "aux_misframed", "resyncs", "resync_dropped")

    def __init__(self, transport):
        self.transport = transport
        self.repair = transport.repair

    def counters(self):
        totals = dict.fromkeys(self.KEYS, 0)
        for st in self.transport.by_this.values():
            for k in self.KEYS:
                totals[k] += st[k]
        return totals

    def snapshot(self):
        return self.transport.snapshot()


@dataclass
class _Session:
    lib: object
    backend: str
    dylib: str
    slide: int
    stats: object
    status: object
    native: object = None


_SESSION = None


def _load(dylib, backend, filter_repair, promote):
    """Load the library once per process, with the requested backend installed."""
    global _SESSION
    dylib = os.path.abspath(dylib)
    if _SESSION is not None:
        if (_SESSION.dylib, _SESSION.backend) != (dylib, backend):
            raise RuntimeError(
                f"this process already loaded {_SESSION.dylib} with the "
                f"{_SESSION.backend} backend; one library and backend per process")
        _SESSION.stats.repair.mode = filter_repair
        return _SESSION
    if backend not in ("native", "python"):
        raise ValueError("backend must be 'native' or 'python'")
    anchor = T.symbol_addresses(dylib, {T.ANCHOR_SYM})[T.ANCHOR_SYM]
    if backend == "native":
        import sm_native as N
        lib, native = N.install(dylib, filter_mode=filter_repair, promote=promote)
        bind(lib)
        slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - anchor
        stats = _NativeStats(native, N)
    else:
        native = None
        vtable = T.symbol_addresses(dylib, {T.VTABLE_SYM})[T.VTABLE_SYM]
        lib = ctypes.CDLL(dylib)
        bind(lib)
        slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - anchor
        transport = T.Transport(T.load_filter_repair(dylib, slide, filter_repair))
        T.patch_vtable(vtable + slide, transport)
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sem_shim.dylib")
        if not os.path.exists(shim):
            raise RuntimeError("sem_shim.dylib not found; build it with make")
        T.patch_semaphores(dylib, slide, shim)
        stats = _PythonStats(transport)
    _SESSION = _Session(lib, backend, dylib, slide, stats,
                        T.InterfaceStatus(dylib, slide), native)
    return _SESSION


# ---- the instrument ------------------------------------------------------------
class SM200C:
    """One networked SM200C. Opens on construction; use as a context manager."""

    def __init__(self, dylib, host=DEFAULT_HOST, device=DEFAULT_DEVICE,
                 port=DEFAULT_PORT, *, backend="native", filter_repair="design",
                 promote=True):
        """dylib is Signal Hound's libsm_api.dylib. filter_repair is "design"
        (the filters the Linux build would send), "tables" or "off". promote
        lets the native backend raise the library's thread priority."""
        self._s = _load(dylib, backend, filter_repair, promote)
        self.lib = self._s.lib
        handle = ctypes.c_int(-1)
        self._call("smOpenNetworkedDevice", ctypes.byref(handle), host.encode(),
                   device.encode(), port, dev=False)
        self.handle = handle.value
        self._iq = None             # IQInfo of the active stream
        self._sweep = None
        self._segments = None
        self._seg_info = None
        self._sweep_list = None
        self._full_band_samples = None

    # -- plumbing -----------------------------------------------------------------
    def _call(self, name, *args, dev=True, warn=True):
        """Call an API function on this device. Raises on error, warns on warning."""
        fn = getattr(self.lib, name)
        status = fn(self.handle, *args) if dev else fn(*args)
        if status < 0:
            raise SmError(name, status, self.lib.smGetErrorString(status).decode())
        if status > 0 and warn:
            warnings.warn(f"{name}: {self.lib.smGetErrorString(status).decode()}",
                          SmWarning, stacklevel=3)
        return status

    def _get(self, name, ctype):
        v = ctype()
        self._call(name, ctypes.byref(v))
        return v.value

    def close(self):
        if getattr(self, "handle", None) is None:
            return
        try:
            self.lib.smAbort(self.handle)
        finally:
            self.lib.smCloseDevice(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- backend --------------------------------------------------------------
    @property
    def backend(self):
        return self._s.backend

    @property
    def filter_repair(self):
        """The FilterRepair in use; its mode can be changed between configures."""
        return self._s.stats.repair

    def transport_counters(self):
        """Cumulative transport counters since the library was loaded."""
        return self._s.stats.counters()

    def transport_snapshot(self):
        """Transport counters and logs since the last snapshot."""
        return self._s.stats.snapshot()

    @property
    def connection_status(self):
        """The library's sticky connection-lost status: 0, or -6 once any
        transfer or command has failed."""
        return self._s.status.read(self.handle)

    def clear_connection_status(self):
        """Clear the sticky status. Returns what it was."""
        return self._s.status.clear(self.handle)

    # -- device -------------------------------------------------------------
    @property
    def info(self):
        dtype, serial = ctypes.c_int(), ctypes.c_int()
        self._call("smGetDeviceInfo", ctypes.byref(dtype), ctypes.byref(serial))
        major, minor, rev = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self._call("smGetFirmwareVersion", ctypes.byref(major), ctypes.byref(minor),
                   ctypes.byref(rev))
        return DeviceInfo(DEVICE_TYPES.get(dtype.value, str(dtype.value)), serial.value,
                          f"{major.value}.{minor.value}.{rev.value}",
                          self.lib.smGetAPIVersion().decode())

    def diagnostics(self):
        v, a, t = ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
        self._call("smGetDeviceDiagnostics", ctypes.byref(v), ctypes.byref(a),
                   ctypes.byref(t))
        return {"voltage": v.value, "current": a.value, "temperature": t.value}

    def full_diagnostics(self):
        names = ("voltage", "current_input", "current_ocxo", "current_58",
                 "temp_fpga_internal", "temp_fpga_near", "temp_ocxo", "temp_vco",
                 "temp_rf_board_lo", "temp_power_supply")
        buf = (ctypes.c_float * len(names))()
        self._call("smGetFullDeviceDiagnostics", buf)
        return dict(zip(names, buf))

    def sfp_diagnostics(self):
        t, v, tx, rx = (ctypes.c_float() for _ in range(4))
        self._call("smGetSFPDiagnostics", ctypes.byref(t), ctypes.byref(v),
                   ctypes.byref(tx), ctypes.byref(rx))
        return {"temperature": t.value, "voltage": v.value,
                "tx_power_mW": tx.value, "rx_power_mW": rx.value}

    @property
    def calibration_date(self):
        """Date of the last factory calibration (UTC), or None if not recorded."""
        secs = self._get("smGetCalDate", ctypes.c_uint64)
        return (datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).date()
                if secs else None)

    @property
    def mode(self):
        return MODES.get(self._get("smGetCurrentMode", ctypes.c_int), "unknown")

    def abort(self):
        """Stop any measurement and return to idle."""
        self._call("smAbort")
        self._iq = None

    def preset(self):
        """Power-cycle the device to its default state. Reopen afterwards."""
        self._call("smPreset")

    # -- front end ----------------------------------------------------------
    @property
    def ref_level(self):
        """Reference level in dBm. Setting it puts the attenuator on auto."""
        return self._get("smGetRefLevel", ctypes.c_double)

    @ref_level.setter
    def ref_level(self, dbm):
        self._call("smSetAttenuator", SM_AUTO_ATTEN)
        self._call("smSetRefLevel", float(dbm))

    @property
    def attenuator(self):
        """0 to 6 in 5 dB steps, or None for automatic (follows ref_level)."""
        a = self._get("smGetAttenuator", ctypes.c_int)
        return None if a == SM_AUTO_ATTEN else a

    @attenuator.setter
    def attenuator(self, steps):
        self._call("smSetAttenuator", SM_AUTO_ATTEN if steps is None else int(steps))

    @property
    def preselector(self):
        return bool(self._get("smGetPreselector", ctypes.c_int))

    @preselector.setter
    def preselector(self, enabled):
        self._call("smSetPreselector", int(bool(enabled)))

    @property
    def reference(self):
        """Timebase: "internal" or "external" (10 MHz in). Set while idle."""
        v = self._get("smGetReference", ctypes.c_int)
        return {0: "internal", 1: "external"}.get(v, str(v))

    @reference.setter
    def reference(self, which):
        self._call("smSetReference", _choose(REFERENCES, which, "reference"))

    @property
    def reference_out(self):
        """Whether the 10 MHz reference output port is enabled."""
        return bool(self._get("smGetExternalReference", ctypes.c_int))

    @reference_out.setter
    def reference_out(self, enabled):
        self._call("smSetExternalReference", int(bool(enabled)))

    @property
    def power_state(self):
        v = self._get("smGetPowerState", ctypes.c_int)
        return {0: "on", 1: "standby"}.get(v, str(v))

    @power_state.setter
    def power_state(self, state):
        self._call("smSetPowerState", _choose(POWER_STATES, state, "power_state"))

    @property
    def gps_timebase_update(self):
        """Whether the API disciplines the timebase from GPS. Set while idle."""
        return bool(self._get("smGetGPSTimebaseUpdate", ctypes.c_int))

    @gps_timebase_update.setter
    def gps_timebase_update(self, enabled):
        self._call("smSetGPSTimebaseUpdate", int(bool(enabled)))

    @property
    def gps_state(self):
        v = self._get("smGetGPSState", ctypes.c_int)
        return GPS_STATES.get(v, str(v))

    @property
    def gps_holdover(self):
        """(holdover value newer than factory cal, its time in seconds since epoch)."""
        using, when = ctypes.c_int(), ctypes.c_uint64()
        self._call("smGetGPSHoldoverInfo", ctypes.byref(using), ctypes.byref(when))
        return bool(using.value), when.value

    # -- sweeps -------------------------------------------------------------
    def configure_sweep(self, *, center=None, span=None, start=None, stop=None,
                        rbw=100e3, vbw=None, sweep_time=0.001, detector="minmax",
                        video_units="log", scale="dBm", window="flattop", speed="auto",
                        spur_reject=False):
        """Set up and start swept analysis. Give center and span, or start and stop.
        vbw defaults to rbw. Returns what the device will actually sweep."""
        if not ((center is not None and span is not None and start is None and stop is None)
                or (start is not None and stop is not None and center is None and span is None)):
            raise ValueError("give center and span, or start and stop")
        self._call("smSetSweepSpeed", _choose(SPEEDS, speed, "speed"))
        if center is not None:
            self._call("smSetSweepCenterSpan", float(center), float(span))
        else:
            self._call("smSetSweepStartStop", float(start), float(stop))
        self._call("smSetSweepCoupling", float(rbw), float(vbw or rbw), float(sweep_time))
        self._call("smSetSweepDetector", _choose(DETECTORS, detector, "detector"),
                   _choose(VIDEO_UNITS, video_units, "video_units"))
        self._call("smSetSweepScale", _choose(SCALES, scale, "scale"))
        self._call("smSetSweepWindow", _choose(WINDOWS, window, "window"))
        self._call("smSetSweepSpurReject", int(bool(spur_reject)))
        self._call("smConfigure", 1)
        self._iq = None
        rbw_, vbw_, start_, binsize = (ctypes.c_double() for _ in range(4))
        size = ctypes.c_int()
        self._call("smGetSweepParameters", ctypes.byref(rbw_), ctypes.byref(vbw_),
                   ctypes.byref(start_), ctypes.byref(binsize), ctypes.byref(size))
        self._sweep = SweepInfo(rbw_.value, vbw_.value, start_.value, binsize.value,
                                size.value)
        return self._sweep

    def _sweep_buffers(self):
        if self._sweep is None:
            raise RuntimeError("configure_sweep first")
        n = self._sweep.size
        return np.empty(n, np.float32), np.empty(n, np.float32)

    def sweep(self):
        """One sweep, blocking. Uses queue position 0."""
        lo, hi = self._sweep_buffers()
        ts = ctypes.c_int64()
        status = self._call("smGetSweep", lo.ctypes.data, hi.ctypes.data,
                            ctypes.byref(ts), warn=False)
        return Sweep(self._sweep.freqs, lo, hi, ts.value, status)

    def sweeps(self, count=None, depth=4):
        """Yield sweeps continuously, keeping depth of them queued in the device.
        count None runs until the caller stops iterating."""
        if not 1 <= depth < SWEEP_QUEUE:
            raise ValueError(f"depth must be 1 to {SWEEP_QUEUE - 1}")
        positions = list(range(1, depth + 1))       # 0 is smGetSweep's
        started = 0
        for pos in positions:
            if count is not None and started >= count:
                break
            self._call("smStartSweep", pos)
            started += 1
        done, i = 0, 0
        try:
            while done < started:
                pos = positions[i % depth]
                lo, hi = self._sweep_buffers()
                ts = ctypes.c_int64()
                status = self._call("smFinishSweep", pos, lo.ctypes.data, hi.ctypes.data,
                                    ctypes.byref(ts), warn=False)
                done += 1
                i += 1
                if count is None or started < count:
                    self._call("smStartSweep", pos)
                    started += 1
                yield Sweep(self._sweep.freqs, lo, hi, ts.value, status)
        finally:
            if done < started:                       # stopped early: drop the queue
                self.lib.smAbort(self.handle)
                self._call("smConfigure", 1)

    # -- I/Q streaming ----------------------------------------------------------
    def configure_iq(self, *, center, decimation=1, bandwidth=None,
                     data_type="complex64", base_rate="native", queue_ms=None,
                     trigger_edge=None):
        """Set up and start I/Q streaming.

        decimation is a power of two from 1 (200 MS/s) to 8192. bandwidth
        defaults to 80% of the output rate. queue_ms (5.24 to 41.9) trades
        tolerance of interruptions against retune speed; it defaults to the
        maximum below decimation 8. trigger_edge "rising" or "falling" reports
        external triggers through read_iq(triggers=...).
        """
        self._call("smSetIQBaseSampleRate", _choose(BASE_RATES, base_rate, "base_rate"))
        base = 200e6 if base_rate == "native" else 122.88e6
        self._call("smSetIQDataType", _choose(DATA_TYPES, data_type, "data_type"))
        self._call("smSetIQCenterFreq", float(center))
        self._call("smSetIQSampleRate", int(decimation))
        self._call("smSetIQBandwidth", 0, float(bandwidth or base / decimation * 0.8))
        if trigger_edge is not None:
            self._call("smSetIQExtTriggerEdge", _choose(EDGES, trigger_edge, "trigger_edge"))
        if queue_ms is None and decimation < 8:
            queue_ms = 16 * TRANSFER_MS
        if queue_ms is not None:
            self._call("smSetIQQueueSize", float(queue_ms))
        self._call("smConfigure", 3)
        rate, bw = ctypes.c_double(), ctypes.c_double()
        self._call("smGetIQParameters", ctypes.byref(rate), ctypes.byref(bw))
        actual = self._get("smGetIQCenterFreq", ctypes.c_double)
        corr = self._get("smGetIQCorrection", ctypes.c_float)
        self._iq = IQInfo(actual, rate.value, bw.value, corr, int(decimation), data_type)
        self._first_read = True
        return self._iq

    @property
    def iq_info(self):
        return self._iq

    def iq_buffer(self, count):
        """An empty array the right shape and type for count samples of the stream."""
        if self._iq.data_type == "complex64":
            return np.empty(count, np.complex64)
        return np.empty((count, 2), np.int16)

    def read_iq(self, count, *, purge=None, out=None, triggers=0):
        """Read count samples of the active stream, blocking until they arrive.

        purge discards anything buffered first; it defaults to True for the
        first read after configure_iq. out, if given, is filled in place (an
        array from a previous block, or of the right dtype and shape) to avoid
        allocating at high rates. triggers is how many external trigger
        positions to collect. A sync error does not raise: the samples arrived
        and the block says so.
        """
        if self._iq is None:
            raise RuntimeError("configure_iq first")
        if purge is None:
            purge = self._first_read
        buf = self.iq_buffer(count) if out is None else out
        want = np.complex64 if self._iq.data_type == "complex64" else np.int16
        if (buf.dtype != want or buf.shape[0] < count or not buf.flags.c_contiguous
                or (want == np.int16 and buf.shape[1:] != (2,))):
            raise ValueError(f"out must be a contiguous {np.dtype(want).name} array "
                             f"of at least {count} samples"
                             + (" with shape (n, 2)" if want == np.int16 else ""))
        trig = np.zeros(triggers, np.float64) if triggers else None
        ns, loss, remaining = ctypes.c_int64(), ctypes.c_int(), ctypes.c_int()
        status = self.lib.smGetIQ(self.handle, buf.ctypes.data, count,
                                  trig.ctypes.data if trig is not None else None,
                                  triggers, ctypes.byref(ns), int(bool(purge)),
                                  ctypes.byref(loss), ctypes.byref(remaining))
        if status < 0 and status != SYNC_ERR:
            raise SmError("smGetIQ", status, self.lib.smGetErrorString(status).decode())
        self._first_read = False
        return IQBlock(buf[:count], ns.value, bool(loss.value), remaining.value,
                       self._iq.correction, max(status, 0), status == SYNC_ERR, trig)

    def stream_iq(self, block=None, count=None):
        """Yield IQBlocks of block samples (default about 5 ms), count of them or
        until the caller stops."""
        if self._iq is None:
            raise RuntimeError("configure_iq first")
        if block is None:
            block = max(32768, min(1 << 20, int(self._iq.sample_rate * 0.005)))
        n = 0
        while count is None or n < count:
            yield self.read_iq(block)
            n += 1

    # -- I/Q sweep list -------------------------------------------------------
    def configure_iq_sweep_list(self, steps, *, data_type="complex64", corrected=True):
        """Set up a frequency-hopping I/Q capture: a list of SweepListStep.
        corrected False returns full-scale data. Returns the per-step corrections.
        Untested on this transport."""
        steps = list(steps)
        if not steps:
            raise ValueError("at least one step")
        self._call("smSetIQSweepListDataType", _choose(DATA_TYPES, data_type, "data_type"))
        self._call("smSetIQSweepListCorrected", int(bool(corrected)))
        self._call("smSetIQSweepListSteps", len(steps))
        for i, s in enumerate(steps):
            self._call("smSetIQSweepListFreq", i, float(s.freq))
            if s.atten is None:
                self._call("smSetIQSweepListAtten", i, SM_AUTO_ATTEN)
                self._call("smSetIQSweepListRef", i, float(s.ref_level))
            else:
                self._call("smSetIQSweepListAtten", i, int(s.atten))
            self._call("smSetIQSweepListSampleCount", i, int(s.samples))
        self._call("smConfigure", 6)
        self._iq = None
        corr = np.zeros(len(steps), np.float32)
        self._call("smIQSweepListGetCorrections", corr.ctypes.data)
        self._sweep_list = (steps, data_type, corr.tolist())
        return corr.tolist()

    def _sweep_list_buffers(self):
        steps, data_type, _ = self._sweep_list
        total = sum(s.samples for s in steps)
        buf = (np.empty(total, np.complex64) if data_type == "complex64"
               else np.empty((total, 2), np.int16))
        return buf, np.zeros(len(steps), np.int64)

    def _split_sweep_list(self, buf, ts):
        steps, _, corr = self._sweep_list
        edges = np.cumsum([0] + [s.samples for s in steps])
        return IQSweep([s.freq for s in steps],
                       [buf[a:b] for a, b in zip(edges[:-1], edges[1:])],
                       ts.tolist(), corr)

    def iq_sweep_list(self):
        """One I/Q sweep across every configured step, blocking."""
        if self._sweep_list is None:
            raise RuntimeError("configure_iq_sweep_list first")
        buf, ts = self._sweep_list_buffers()
        self._call("smIQSweepListGetSweep", buf.ctypes.data, ts.ctypes.data)
        return self._split_sweep_list(buf, ts)

    def iq_sweep_lists(self, count=None, depth=4):
        """Yield I/Q sweeps continuously with depth of them queued."""
        if self._sweep_list is None:
            raise RuntimeError("configure_iq_sweep_list first")
        if not 1 <= depth <= SWEEP_QUEUE:
            raise ValueError(f"depth must be 1 to {SWEEP_QUEUE}")
        pending = []                                 # (pos, buf, ts) in start order
        started = done = 0

        def start(pos):
            buf, ts = self._sweep_list_buffers()
            self._call("smIQSweepListStartSweep", pos, buf.ctypes.data, ts.ctypes.data)
            pending.append((pos, buf, ts))

        try:
            for pos in range(depth):
                if count is not None and started >= count:
                    break
                start(pos)
                started += 1
            while pending:
                pos, buf, ts = pending.pop(0)
                self._call("smIQSweepListFinishSweep", pos)
                done += 1
                if count is None or started < count:
                    start(pos)
                    started += 1
                yield self._split_sweep_list(buf, ts)
        finally:
            if pending:                              # stopped early: drop the queue
                self.lib.smAbort(self.handle)

    # -- segmented I/Q --------------------------------------------------------
    def configure_segmented(self, *, center, segments, data_type="complex64",
                            video_trigger=None, external_trigger_edge=None,
                            fmt_mask=None):
        """Set up segmented captures: a list of Segment, each with its own trigger.

        video_trigger is (level_dBm, "rising" or "falling"); external_trigger_edge
        is "rising" or "falling"; fmt_mask is (fft_size, freqs_Hz, levels_dBm)
        for the frequency mask trigger. Documented for the SM200B and SM435B;
        untested here.
        """
        segments = list(segments)
        self._call("smSetSegIQDataType", _choose(DATA_TYPES, data_type, "data_type"))
        self._call("smSetSegIQCenterFreq", float(center))
        if video_trigger is not None:
            level, edge = video_trigger
            self._call("smSetSegIQVideoTrigger", float(level), _choose(EDGES, edge, "edge"))
        if external_trigger_edge is not None:
            self._call("smSetSegIQExtTrigger",
                       _choose(EDGES, external_trigger_edge, "external_trigger_edge"))
        if fmt_mask is not None:
            fft, freqs, levels = fmt_mask
            f = np.ascontiguousarray(freqs, np.float64)
            a = np.ascontiguousarray(levels, np.float64)
            if len(f) != len(a):
                raise ValueError("fmt_mask needs as many levels as frequencies")
            self._call("smSetSegIQFMTParams", int(fft), f.ctypes.data, a.ctypes.data, len(f))
        self._call("smSetSegIQSegmentCount", len(segments))
        for i, s in enumerate(segments):
            self._call("smSetSegIQSegment", i, _choose(TRIGGERS, s.trigger, "trigger"),
                       int(s.pre_trigger), int(s.capture_size), float(s.timeout))
        self._call("smConfigure", 5)
        self._iq = None
        rate, bw = ctypes.c_double(), ctypes.c_double()
        self._call("smGetIQParameters", ctypes.byref(rate), ctypes.byref(bw))
        corr = self._get("smGetIQCorrection", ctypes.c_float)
        maxc = self._get("smSegIQGetMaxCaptures", ctypes.c_int)
        self._segments = (segments, data_type)
        self._seg_info = SegmentedInfo(float(center), rate.value, bw.value, corr, maxc,
                                       data_type)
        return self._seg_info

    def start_segmented(self, capture=0):
        """Queue capture number capture (0 to max_captures - 1). Returns at once."""
        if self._segments is None:
            raise RuntimeError("configure_segmented first")
        self._call("smSegIQCaptureStart", int(capture))

    def segmented_ready(self, capture=0):
        done = ctypes.c_int()
        self._call("smSegIQCaptureWaitAsync", int(capture), ctypes.byref(done))
        return bool(done.value)

    def collect_segmented(self, capture=0):
        """Wait for a started capture, read every segment, and free it."""
        segments, data_type = self._segments
        self._call("smSegIQCaptureWait", int(capture))
        out = []
        try:
            for i, s in enumerate(segments):
                n = s.length
                buf = (np.empty(n, np.complex64) if data_type == "complex64"
                       else np.empty((n, 2), np.int16))
                self._call("smSegIQCaptureRead", int(capture), i, buf.ctypes.data, 0, n)
                ns, timed_out = ctypes.c_int64(), ctypes.c_int()
                self._call("smSegIQCaptureTime", int(capture), i, ctypes.byref(ns))
                self._call("smSegIQCaptureTimeout", int(capture), i, ctypes.byref(timed_out))
                out.append(SegmentData(buf, ns.value, bool(timed_out.value)))
        finally:
            self._call("smSegIQCaptureFinish", int(capture))
        return out

    def capture_segmented(self, capture=0):
        """Start, wait for and read one capture: a SegmentData per segment."""
        self.start_segmented(capture)
        return self.collect_segmented(capture)

    @staticmethod
    def lte_resample(iq, clear=True):
        """Resample 250 MS/s segmented-capture complex64 to 245.76 MS/s for LTE.
        clear starts a new delay line; set False to continue a previous call."""
        if _SESSION is None:
            raise RuntimeError("open a device first, to load the library")
        x = np.ascontiguousarray(iq, np.complex64)
        y = np.empty_like(x)
        n = ctypes.c_int(len(y))
        status = _SESSION.lib.smSegIQLTEResample(x.ctypes.data, len(x), y.ctypes.data,
                                                  ctypes.byref(n), bool(clear))
        if status < 0:
            raise SmError("smSegIQLTEResample", status,
                          _SESSION.lib.smGetErrorString(status).decode())
        return y[:n.value]

    # -- full band ------------------------------------------------------------
    def configure_full_band(self, *, atten=0, corrected=True, samples=32768,
                            trigger="immediate", video_level=None, trigger_timeout=None):
        """Settings for 500 MS/s full-band captures, which configure themselves
        when taken and leave the device idle. atten is 0 to 6 (no auto);
        samples 2048 to 32768; trigger "immediate", "video" or "external";
        video_level in dBFS; trigger_timeout 0 to 1 s. Data is full scale.
        Untested on this transport."""
        self._call("smSetIQFullBandAtten", int(atten))
        self._call("smSetIQFullBandCorrected", int(bool(corrected)))
        self._call("smSetIQFullBandSamples", int(samples))
        if trigger not in ("immediate", "video", "external"):
            raise ValueError("trigger must be immediate, video or external")
        self._call("smSetIQFullBandTriggerType", TRIGGERS[trigger])
        if video_level is not None:
            self._call("smSetIQFullBandVideoTrigger", float(video_level))
        if trigger_timeout is not None:
            self._call("smSetIQFullBandTriggerTimeout", float(trigger_timeout))
        self._full_band_samples = int(samples)

    @staticmethod
    def full_band_index(freq):
        """The full-band index whose centre is nearest freq, in Hz."""
        return max(0, int(round(freq / FULL_BAND_STEP_HZ)) - 1)

    def full_band(self, index):
        """One full-band capture centred on (index + 1) * 39.0625 MHz."""
        if self._full_band_samples is None:
            raise RuntimeError("configure_full_band first")
        self.abort()
        buf = np.empty(self._full_band_samples, np.complex64)
        self._call("smGetIQFullBand", buf.ctypes.data, int(index))
        return FullBandCapture(index, (index + 1) * FULL_BAND_STEP_HZ, buf)

    def full_band_sweep(self, start_index, step, steps):
        """Full-band captures at start_index + n * step for n in 0 to steps - 1
        (1 to 64 steps). Always immediately triggered."""
        if self._full_band_samples is None:
            raise RuntimeError("configure_full_band first")
        self.abort()
        n = self._full_band_samples
        buf = np.empty(n * steps, np.complex64)
        self._call("smGetIQFullBandSweep", buf.ctypes.data, int(start_index),
                   int(step), int(steps))
        return [FullBandCapture(start_index + k * step,
                                (start_index + k * step + 1) * FULL_BAND_STEP_HZ,
                                buf[k * n:(k + 1) * n]) for k in range(steps)]

    # -- escape hatch -----------------------------------------------------------
    def call(self, name, *args):
        """Call any other SM API function on this device by name, with the
        handle supplied. Set its argtypes on self.lib first if bind() did not."""
        return self._call(name, *args)


__all__ = ["SM200C", "SmError", "SmWarning", "DeviceInfo", "SweepInfo", "Sweep",
           "IQInfo", "IQBlock", "SweepListStep", "IQSweep", "Segment", "SegmentedInfo",
           "SegmentData", "FullBandCapture", "DEFAULT_HOST", "DEFAULT_DEVICE",
           "DEFAULT_PORT"]
