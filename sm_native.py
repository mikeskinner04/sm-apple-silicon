#!/usr/bin/env python3
"""Install the native C transport (sm_native) into libsm_api at runtime.

This is the fast backend for sustained decimation-1 streaming. Where
sm_transport.py runs each transport method as a Python callback, this points the
same vtable slots straight at the exported C functions in libsmnative.dylib, so no
Python runs on the data path at all. A dedicated receiver thread inside the
library drains the socket. It also redirects the broken semaphore imports to the
native shim in the same dylib, and registers a callback so impulse filter
uploads are repaired by the same Python code the other backend uses. Filter
uploads happen only while configuring, so no Python runs on the data path.

Use this instead of sm_transport.py when you need decimation 1. For sweeps and
higher decimations either backend works; the Python one is easier to instrument.

    python3 sm_native.py <libsm_api.dylib> <host-ip> <dev-ip> <port>

Build the dylib first:  make -C native   (or see the Makefile beside this file)
"""

import ctypes
import os
import sys

import sm_transport as T   # reuse its Mach-O parsing and slot map


def native_lib_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("libsmnative.dylib", "native/libsmnative.dylib",
                 "libsmnative.so", "native/libsmnative.so"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    sys.exit("libsmnative.dylib not found; build it with: make -C native")


# Each vtable slot maps to the C export with the matching ABI. The signatures
# are already defined in sm_transport.SIGS keyed by slot; we reuse them so the
# two backends can never drift apart.
SLOT_TO_EXPORT = {
    T.SLOT_ALLOCATE: "smn_allocate",
    T.SLOT_DEALLOCATE: "smn_deallocate",
    T.SLOT_BEGIN_CMD: "smn_begin_cmd",
    T.SLOT_FINISH_CMD: "smn_finish_cmd",
    T.SLOT_BEGIN_DATA: "smn_begin_data",
    T.SLOT_BEGIN_DATA_BUF: "smn_begin_data_buf",
    T.SLOT_FINISH_DATA: "smn_finish_data",
    T.SLOT_DATA: "smn_data",
    T.SLOT_TIMED_OUT: "smn_timed_out",
    T.SLOT_XFER_LEN: "smn_xfer_len",
}

# Semaphore imports point at the native shim in the same dylib.
SEM_MAP = {"_sem_init": "smn_sem_init", "_sem_wait": "smn_sem_wait",
           "_sem_post": "smn_sem_post", "_sem_destroy": "smn_sem_destroy"}


def patch_vtable_native(vtable_addr, native):
    libc = ctypes.CDLL(None)
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    page = 16384
    base = vtable_addr & ~(page - 1)
    if libc.mprotect(ctypes.c_void_p(base), page * 2, 1 | 2) != 0:
        sys.exit("mprotect failed; cannot make the vtable writable")
    for slot, export in SLOT_TO_EXPORT.items():
        fn = getattr(native, export)
        addr = ctypes.cast(fn, ctypes.c_void_p).value
        ctypes.c_void_p.from_address(vtable_addr + slot).value = addr
    print(f"patched {len(SLOT_TO_EXPORT)} vtable slots to native at {vtable_addr:#x}")


def patch_semaphores_native(path, slide, native):
    slots = T.got_slots(path, set(SEM_MAP))
    missing = set(SEM_MAP) - slots.keys()
    if missing:
        sys.exit(f"no __got slots for {sorted(missing)}")
    for imported, export in SEM_MAP.items():
        target = ctypes.cast(getattr(native, export), ctypes.c_void_p).value
        ctypes.c_void_p.from_address(slots[imported] + slide).value = target
    print(f"redirected {len(SEM_MAP)} semaphore imports to native shim")


REPAIR_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_uint32), ctypes.c_int)


def register_filter_repair(native, repair):
    """Have the C send path hand impulse filter uploads to repair (a FilterRepair).

    The callback edits the packet in place and returns how many uploads it
    replaced, which the C side adds to its filter_repairs counter.
    """
    def callback(this, words, nwords):
        try:
            original = words[:nwords]
            fixed, report = repair.apply(this, original)
            for i, (a, b) in enumerate(zip(original, fixed)):
                if a != b:
                    words[i] = b & 0xFFFFFFFF
            return sum(1 for r in report if r.get("repaired"))
        except Exception as e:                  # never let an error unwind into C
            print(f"  ! filter repair failed: {e}")
            return 0

    cb = REPAIR_CB(callback)
    T.KEEP_ALIVE.append(cb)
    native.smn_set_repair_callback.argtypes = [ctypes.c_void_p]
    native.smn_set_repair_callback(ctypes.cast(cb, ctypes.c_void_p))


def declare(native):
    """Set return and argument types so ctypes builds correct thunks."""
    native.smn_allocate.restype = ctypes.c_bool
    native.smn_allocate.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_char_p, ctypes.c_uint16]
    native.smn_finish_cmd.restype = ctypes.c_int
    native.smn_finish_data.restype = ctypes.c_int
    native.smn_data.restype = ctypes.c_void_p
    native.smn_timed_out.restype = ctypes.c_bool
    native.smn_xfer_len.restype = ctypes.c_int
    native.smn_version.restype = ctypes.c_char_p
    native.smn_stats_size.restype = ctypes.c_int
    native.smn_set_promote.argtypes = [ctypes.c_int]


STAT_U64 = ["datagrams", "payload_bytes", "lost", "gaps", "resets", "strays",
            "short_datagrams", "timeouts", "transfers", "short_transfers",
            "filter_repairs", "commands", "aux_lost", "queue_empty",
            "lib_promotions", "stale_flushed", "aux_misframed", "resyncs",
            "resync_dropped", "first_misframe", "max_outstanding"]
STAT_I32 = ["rcvbuf", "rx_sched", "lib_policy_low", "lib_prio_low",
            "lib_policy_after", "lib_prio_after"]


RX_SCHED = {0: "default scheduling", 1: "user-interactive QoS", 2: "real-time"}


class Stats(ctypes.Structure):
    """Mirror of smn_stats_t; field order must match the C."""
    _fields_ = ([(n, ctypes.c_uint64) for n in STAT_U64] +
                [(n, ctypes.c_int32) for n in STAT_I32])

    def as_dict(self):
        return {n: getattr(self, n) for n, _ in self._fields_}


def stats(native, owner=None):
    native.smn_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(Stats)]
    s = Stats()
    native.smn_get_stats(owner, ctypes.byref(s))
    return s


def install(libsm_path, filter_mode="design", promote=True):
    """Load libsm_api, install the native backend, and return (lib, native).

    Importable entry point for sm_iq_capture. After this returns,
    smOpenNetworkedDevice and the I/Q calls work through the native transport.
    filter_mode is "design", "tables" or "off"; native.filter_repair is the
    FilterRepair, whose mode can be changed later. promote=False leaves the
    library's own thread priorities alone, for A/B runs.
    """
    native = ctypes.CDLL(native_lib_path())
    declare(native)
    if native.smn_stats_size() != ctypes.sizeof(Stats):
        sys.exit(f"stats struct mismatch: C {native.smn_stats_size()} "
                 f"vs Python {ctypes.sizeof(Stats)}")

    addrs = T.symbol_addresses(libsm_path, {T.VTABLE_SYM, T.ANCHOR_SYM})
    lib = ctypes.CDLL(libsm_path)
    lib.smGetAPIVersion.restype = ctypes.c_char_p
    slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - addrs[T.ANCHOR_SYM]
    print(f"api {lib.smGetAPIVersion().decode()}  slide {slide:#x}  "
          f"backend {native.smn_version().decode()}")

    native.smn_set_promote(1 if promote else 0)
    patch_vtable_native(addrs[T.VTABLE_SYM] + slide, native)
    patch_semaphores_native(libsm_path, slide, native)
    native.filter_repair = T.load_filter_repair(libsm_path, slide, filter_mode)
    register_filter_repair(native, native.filter_repair)
    return lib, native


def main():
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    path, host_ip, dev_ip, port = sys.argv[1:5]
    lib, native = install(path)

    lib.smOpenNetworkedDevice.restype = ctypes.c_int
    lib.smOpenNetworkedDevice.argtypes = [ctypes.POINTER(ctypes.c_int),
                                          ctypes.c_char_p, ctypes.c_char_p,
                                          ctypes.c_uint16]
    lib.smGetErrorString.restype = ctypes.c_char_p
    lib.smGetErrorString.argtypes = [ctypes.c_int]

    dev = ctypes.c_int(-1)
    rc = lib.smOpenNetworkedDevice(ctypes.byref(dev), host_ip.encode(),
                                   dev_ip.encode(), int(port))
    print(f"smOpenNetworkedDevice -> {rc} ({lib.smGetErrorString(rc).decode()})"
          f"  handle {dev.value}")
    if rc == 0:
        s = stats(native)
        print(f"negotiated rcvbuf {s.rcvbuf} bytes, "
              f"receiver thread {RX_SCHED.get(s.rx_sched, s.rx_sched)}")
        print("device open through the native backend; hand off to sm_iq_capture "
              "for a real capture")


if __name__ == "__main__":
    main()
