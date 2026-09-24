#!/usr/bin/env python3
"""Capture I/Q from an SM200C on macOS through the patched transport.

Reuses sm_transport.py (Python backend) or sm_native.py (--native, the C
backend) for the vtable and semaphore patches, then drives the normal SM API:
configure, smConfigure(smModeIQStreaming), smGetIQ in a loop. Signatures are
taken from sm_api.h, not guessed.

    python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib \
        --host 192.168.2.2 --device 192.168.2.10 --port 51665 \
        --center 1e9 --decimation 64 --seconds 2

Decimation 1 (200 MS/s) needs the native backend; see docs/guide.md:

    python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --native \
        --decimation 1 --short --seconds 10 --discard

Each capture writes raw interleaved samples to --out and a JSON sidecar beside
it recording the settings, the transport's loss counters for that capture, and
the sample positions of any zero-filled holes.
"""

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sm_transport as T

# sm_api.h
SM_AUTO_ATTEN = -1
smDataType32fc, smDataType16sc = 0, 1
smModeIdle, smModeIQStreaming = 0, 3
smIQStreamSampleRateNative = 0
smFalse, smTrue = 0, 1
NETWORKED_BASE_RATE = 200e6
TRANSFER_MS = 2.62144          # one request; the queue is 2 to 16 of these
SAMPLES_PER_DATAGRAM = 2048    # 8192-byte payload of 16-bit complex

DEVICE_TYPES = {0: "SM200A", 1: "SM200B", 2: "SM200C", 3: "SM435B", 4: "SM435C"}


def bind(lib):
    """Prototypes straight out of sm_api.h."""
    c_int, c_double, c_float = ctypes.c_int, ctypes.c_double, ctypes.c_float
    p = ctypes.POINTER

    lib.smGetErrorString.restype = ctypes.c_char_p
    lib.smGetErrorString.argtypes = [c_int]
    lib.smGetAPIVersion.restype = ctypes.c_char_p

    lib.smOpenNetworkedDevice.argtypes = [p(c_int), ctypes.c_char_p,
                                          ctypes.c_char_p, ctypes.c_uint16]
    lib.smCloseDevice.argtypes = [c_int]
    lib.smAbort.argtypes = [c_int]
    lib.smGetDeviceInfo.argtypes = [c_int, p(c_int), p(c_int)]
    lib.smGetFirmwareVersion.argtypes = [c_int, p(c_int), p(c_int), p(c_int)]
    lib.smGetDeviceDiagnostics.argtypes = [c_int, p(c_float), p(c_float), p(c_float)]
    lib.smGetSFPDiagnostics.argtypes = [c_int, p(c_float), p(c_float),
                                        p(c_float), p(c_float)]

    lib.smSetAttenuator.argtypes = [c_int, c_int]
    lib.smSetRefLevel.argtypes = [c_int, c_double]
    lib.smSetIQBaseSampleRate.argtypes = [c_int, c_int]
    lib.smSetIQDataType.argtypes = [c_int, c_int]
    lib.smSetIQCenterFreq.argtypes = [c_int, c_double]
    lib.smGetIQCenterFreq.argtypes = [c_int, p(c_double)]
    lib.smSetIQQueueSize.argtypes = [c_int, c_float]   # float, not double
    lib.smSetIQSampleRate.argtypes = [c_int, c_int]
    lib.smSetIQBandwidth.argtypes = [c_int, c_int, c_double]
    lib.smConfigure.argtypes = [c_int, c_int]
    lib.smGetIQParameters.argtypes = [c_int, p(c_double), p(c_double)]
    lib.smGetIQCorrection.argtypes = [c_int, p(c_float)]
    lib.smGetIQ.argtypes = [c_int, ctypes.c_void_p, c_int, p(c_double), c_int,
                            p(ctypes.c_int64), c_int, p(c_int), p(c_int)]


def check(lib, status, what):
    if status < 0:
        sys.exit(f"{what} failed: {status} ({lib.smGetErrorString(status).decode()})")
    if status > 0:
        print(f"  {what}: warning {status} ({lib.smGetErrorString(status).decode()})")
    return status


def describe(lib, dev):
    dtype, serial = ctypes.c_int(), ctypes.c_int()
    lib.smGetDeviceInfo(dev, ctypes.byref(dtype), ctypes.byref(serial))
    major, minor, rev = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    lib.smGetFirmwareVersion(dev, ctypes.byref(major), ctypes.byref(minor),
                             ctypes.byref(rev))
    print(f"device   {DEVICE_TYPES.get(dtype.value, dtype.value)} "
          f"serial {serial.value} firmware {major.value}.{minor.value}.{rev.value}")

    volts, amps, temp = (ctypes.c_float() for _ in range(3))
    if lib.smGetDeviceDiagnostics(dev, ctypes.byref(volts), ctypes.byref(amps),
                                  ctypes.byref(temp)) == 0:
        print(f"health   {volts.value:.2f} V  {amps.value:.2f} A  {temp.value:.1f} C")

    st, sv, tx, rx = (ctypes.c_float() for _ in range(4))
    if lib.smGetSFPDiagnostics(dev, ctypes.byref(st), ctypes.byref(sv),
                               ctypes.byref(tx), ctypes.byref(rx)) == 0:
        print(f"sfp      {st.value:.1f} C  {sv.value:.2f} V  "
              f"tx {tx.value:.3f} mW  rx {rx.value:.3f} mW")


def raise_thread_qos():
    """Put the calling thread in the user-interactive QoS band on macOS, so the
    thread draining smGetIQ runs on a performance core. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    try:
        libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libsys.pthread_set_qos_class_self_np(0x21, 0)   # QOS_CLASS_USER_INTERACTIVE
    except (OSError, AttributeError):
        pass


def transport_counters(native, transport):
    """Cumulative transport counters, from whichever backend is in use."""
    if native is not None:
        import sm_native as N
        return N.stats(native).as_dict()
    totals = {}
    for st in transport.by_this.values():
        for k in ("datagrams", "lost", "gaps", "resets"):
            totals[k] = totals.get(k, 0) + st[k]
    return totals


def find_holes(path, short, min_run):
    """Sample positions of zero-filled runs of at least min_run samples.

    The native transport fills each lost datagram with zeros in place, so a
    loss shows up in the capture as an exact run of 0+0j samples, 2048 per
    datagram at hardware decimation. Receiver noise makes such runs impossible
    in real data. Returns [[first_sample, length], ...].
    """
    import numpy as np
    x = np.memmap(path, dtype=np.int16 if short else np.float32, mode="r")
    x = x[: len(x) // 2 * 2].reshape(-1, 2)
    holes, pending, step = [], None, 1 << 22
    for off in range(0, len(x), step):
        blk = x[off:off + step]
        zero = (~blk.any(axis=1)).astype(np.int8)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], zero, [0]))))
        starts, ends = edges[0::2], edges[1::2]
        if pending is not None and (len(starts) == 0 or starts[0] != 0):
            if off - pending >= min_run:           # run ended on the boundary
                holes.append([pending, off - pending])
            pending = None
        for s, e in zip(starts.tolist(), ends.tolist()):
            first = off + s
            if s == 0 and pending is not None:
                first, pending = pending, None
            if e == len(blk):                      # may continue in the next block
                pending = first
                continue
            if off + e - first >= min_run:
                holes.append([first, off + e - first])
    if pending is not None and len(x) - pending >= min_run:
        holes.append([pending, len(x) - pending])
    return holes


def capture(lib, dev, out, total, rate, bytes_per_sample, discard):
    """Read `total` samples with smGetIQ and write them to `out`.

    A writer thread does the file I/O so the reading thread only ever waits on
    the library. Both release the GIL: ctypes drops it around smGetIQ and
    file.write drops it around the system call. Blocks are about 5 ms, so at
    200 MS/s that is 200 calls a second rather than 6,000.
    """
    raise_thread_qos()
    block = max(32768, min(1 << 20, int(rate * 0.005)))
    free, full = queue.Queue(), queue.Queue()
    for _ in range(8):
        free.put(ctypes.create_string_buffer(block * bytes_per_sample))
    failure = []

    def writer():
        with open(os.devnull if discard else out, "wb") as f:
            while True:
                item = full.get()
                if item is None:
                    return
                buf, nbytes = item
                try:
                    if not discard:
                        f.write(memoryview(buf).cast("B")[:nbytes])
                except OSError as e:
                    failure.append(e)
                free.put(buf)

    w = threading.Thread(target=writer, name="iq-writer")
    w.start()
    ns, first_ns = ctypes.c_int64(), None
    loss, remaining = ctypes.c_int(), ctypes.c_int()
    captured = flags = max_backlog = 0
    started = time.monotonic()
    try:
        while captured < total and not failure:
            n = min(block, total - captured)
            buf = free.get()
            status = lib.smGetIQ(dev, buf, n, None, 0, ctypes.byref(ns),
                                 smTrue if captured == 0 else smFalse,
                                 ctypes.byref(loss), ctypes.byref(remaining))
            if status < 0:
                free.put(buf)
                print(f"  smGetIQ: {status} ({lib.smGetErrorString(status).decode()})")
                break
            if first_ns is None:
                first_ns = ns.value
            flags += bool(loss.value)
            max_backlog = max(max_backlog, remaining.value)
            full.put((buf, n * bytes_per_sample))
            captured += n
    finally:
        full.put(None)
        w.join()
    if failure:
        print(f"  write failed: {failure[0]}")
    return {"samples": captured, "seconds": time.monotonic() - started,
            "first_sample_ns": first_ns, "library_loss_flags": flags,
            "max_backlog_ms": 1e3 * max_backlog / rate}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dylib")
    ap.add_argument("--host", default="192.168.2.2")
    ap.add_argument("--device", default="192.168.2.10")
    ap.add_argument("--port", type=int, default=51665)
    ap.add_argument("--center", default="1e9",
                    help="centre frequency in Hz; comma-separated to retune between captures")
    ap.add_argument("--decimation", type=int, default=64, help="power of two")
    ap.add_argument("--ref-level", type=float, default=-20.0, help="dBm")
    ap.add_argument("--seconds", type=float, default=2.0, help="dwell per frequency")
    ap.add_argument("--queue-ms", type=float, default=None,
                    help="smSetIQQueueSize; smaller means faster retunes")
    ap.add_argument("--short", action="store_true", help="16-bit complex instead of 32-bit float")
    ap.add_argument("--no-filter-repair", action="store_true",
                    help="send the library's own filter uploads unmodified")
    ap.add_argument("--native", action="store_true",
                    help="use the native C backend (needed for decimation 1)")
    ap.add_argument("--discard", action="store_true",
                    help="read the stream but write nothing, to test the link without the disk")
    ap.add_argument("--no-promote", action="store_true",
                    help="native only: leave the library's thread priorities alone (A/B runs)")
    ap.add_argument("--out", default="capture.iq",
                    help="single frequency writes here; several get a -<MHz> suffix")
    args = ap.parse_args()
    centers = [float(c) for c in args.center.split(",")]

    transport = native = None
    if args.native:
        import sm_native as N
        lib, native = N.install(args.dylib, filter_repair=not args.no_filter_repair,
                                promote=not args.no_promote)
        bind(lib)
    else:
        addrs = T.symbol_addresses(args.dylib, {T.VTABLE_SYM, T.ANCHOR_SYM})
        lib = ctypes.CDLL(args.dylib)
        bind(lib)
        slide = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - addrs[T.ANCHOR_SYM]
        transport = T.Transport(T.load_filter_tables(not args.no_filter_repair))
        T.patch_vtable(addrs[T.VTABLE_SYM] + slide, transport)
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sem_shim.dylib")
        if not os.path.exists(shim):
            sys.exit("sem_shim.dylib not found; build it first")
        T.patch_semaphores(args.dylib, slide, shim)

    anchor = T.symbol_addresses(args.dylib, {T.ANCHOR_SYM})[T.ANCHOR_SYM]
    status = T.InterfaceStatus(
        args.dylib, ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value - anchor)

    handle = ctypes.c_int(-1)
    check(lib, lib.smOpenNetworkedDevice(ctypes.byref(handle), args.host.encode(),
                                         args.device.encode(), args.port), "open")
    dev = handle.value
    print()
    describe(lib, dev)

    data_type = smDataType16sc if args.short else smDataType32fc
    bytes_per_sample = 4 if args.short else 8

    check(lib, lib.smSetAttenuator(dev, SM_AUTO_ATTEN), "smSetAttenuator")
    check(lib, lib.smSetRefLevel(dev, args.ref_level), "smSetRefLevel")
    check(lib, lib.smSetIQBaseSampleRate(dev, smIQStreamSampleRateNative),
          "smSetIQBaseSampleRate")
    check(lib, lib.smSetIQDataType(dev, data_type), "smSetIQDataType")
    check(lib, lib.smSetIQSampleRate(dev, args.decimation), "smSetIQSampleRate")
    check(lib, lib.smSetIQBandwidth(dev, smFalse,
                                    NETWORKED_BASE_RATE / args.decimation * 0.8),
          "smSetIQBandwidth")
    if args.queue_ms is None and args.decimation < 8:
        # Below decimation 8 each request is a larger transfer and the stream
        # is faster, so keep the most requests in flight the API allows.
        args.queue_ms = 16 * TRANSFER_MS
        print(f"  queue size {args.queue_ms:.2f} ms (16 requests, the maximum)")
    if args.queue_ms is not None:
        check(lib, lib.smSetIQQueueSize(dev, args.queue_ms), "smSetIQQueueSize")
    hw_dec = min(args.decimation, 8)
    min_hole = max(64, SAMPLES_PER_DATAGRAM * hw_dec // args.decimation)

    for n_freq, center in enumerate(centers):
        # Retuning means setting the frequency and reconfiguring. smConfigure
        # calls smAbort internally, so the engine thread is torn down and
        # rebuilt on every hop.
        # One failed transfer anywhere leaves the library's connection-lost
        # status at -6 for good, and smGetIQ then refuses every call. Start
        # each capture clean and say so if it was set.
        status_in = status.clear(dev)
        if status_in:
            print(f"  connection-lost status was stuck at {status_in} from an earlier "
                  f"failed transfer; cleared")
        tune_start = time.monotonic()
        check(lib, lib.smSetIQCenterFreq(dev, center), "smSetIQCenterFreq")
        check(lib, lib.smConfigure(dev, smModeIQStreaming), "smConfigure")
        tune_ms = (time.monotonic() - tune_start) * 1e3

        rate, bw = ctypes.c_double(), ctypes.c_double()
        check(lib, lib.smGetIQParameters(dev, ctypes.byref(rate), ctypes.byref(bw)),
              "smGetIQParameters")
        actual = ctypes.c_double()
        lib.smGetIQCenterFreq(dev, ctypes.byref(actual))
        scale = ctypes.c_float()
        lib.smGetIQCorrection(dev, ctypes.byref(scale))
        print(f"\ntuned to {actual.value/1e6:.6f} MHz in {tune_ms:.1f} ms")
        print(f"  {rate.value/1e6:.4f} MS/s, {bw.value/1e6:.3f} MHz bandwidth, "
              f"correction {scale.value:.6g}")

        if len(centers) == 1:
            out = args.out
        else:
            stem, ext = os.path.splitext(args.out)
            out = f"{stem}-{actual.value/1e6:.3f}MHz{ext}"

        before = transport_counters(native, transport)
        result = capture(lib, dev, out, int(rate.value * args.seconds), rate.value,
                         bytes_per_sample, args.discard)
        after = transport_counters(native, transport)
        delta = {k: after[k] - before.get(k, 0) for k in after
                 if isinstance(after[k], int) and k not in
                 ("rcvbuf", "rx_sched", "max_outstanding") and not k.startswith("lib_")}

        captured, elapsed = result["samples"], result["seconds"]
        print(f"  captured {captured} samples in {elapsed:.2f} s "
              f"({captured / elapsed / 1e6:.3f} MS/s sustained)"
              f"{'' if args.discard else ' -> ' + out}")
        print(f"  library backlog peaked at {result['max_backlog_ms']:.1f} ms of its 500 ms")

        holes = None
        if native is not None and not args.discard and delta.get("lost", 0):
            try:
                holes = find_holes(out, args.short, min_hole)
            except ImportError:
                print("  numpy not installed; hole positions not scanned")

        problems = []
        if delta.get("lost"):
            problems.append(f"{delta['lost']} datagrams lost in transit "
                            f"({delta.get('aux_lost', 0)} of them aux blocks), zero-filled")
        if delta.get("timeouts"):
            problems.append(f"{delta['timeouts']} transfer timeouts; close and reopen "
                            f"the device before trusting further captures")
        if result["library_loss_flags"]:
            problems.append(f"{result['library_loss_flags']} blocks flagged by the device "
                            f"as sample loss (requests fell behind)")
        if delta.get("resets"):
            problems.append(f"{delta['resets']} counter restarts")
        status_out = status.read(dev)
        if status_out:
            problems.append(f"connection-lost status set to {status_out} during the capture: "
                            f"a transfer or command failed")
        if holes:
            span = sum(h[1] for h in holes)
            problems.append(f"{len(holes)} zero-filled holes, {span} samples, "
                            f"positions in the sidecar")
        print("  clean: no loss anywhere" if not problems else
              "  DATA LOSS:\n    " + "\n    ".join(problems))

        if not args.discard:
            meta = {
                "file": os.path.basename(out),
                "datatype": "ci16_le" if args.short else "cf32_le",
                "sample_rate": rate.value, "bandwidth": bw.value,
                "center_hz": actual.value, "decimation": args.decimation,
                "iq_correction": scale.value,
                "note": "16-bit samples need multiplying by iq_correction" if args.short else "",
                **result,
                "transport": delta,
                "status_in": status_in, "status_out": status_out,
                "holes": holes,
            }
            with open(out + ".json", "w") as f:
                json.dump(meta, f, indent=1)

    if native is not None:
        import sm_native as N
        s = N.stats(native)
        print(f"\nsession: {s.transfers} transfers, {s.datagrams} datagrams, "
              f"{s.payload_bytes / 1e6:.1f} MB, lost {s.lost}, timeouts {s.timeouts}, "
              f"filter repairs {s.filter_repairs}")
        print(f"socket buffer {s.rcvbuf / 2**20:.0f} MB, receiver thread "
              f"{N.RX_SCHED.get(s.rx_sched, s.rx_sched)}, deepest queue "
              f"{s.max_outstanding}, queue ran dry {s.queue_empty} times")
        if s.lib_prio_low:
            policy = {1: "timeshare", 2: "round robin", 4: "fixed (FIFO)"}.get(
                s.lib_policy_low, str(s.lib_policy_low))
            print(f"library thread seen at {policy} priority {s.lib_prio_low}; "
                  + (f"moved to priority {s.lib_prio_after}" if s.lib_promotions
                     else "left alone (--no-promote)"))
    else:
        for st in transport.by_this.values():
            print(f"\nsession: {st['datagrams']} datagrams, gaps {st['gaps']}, "
                  f"lost {st['lost']}, counter resets {st['resets']}")

    lib.smAbort(dev)
    lib.smCloseDevice(dev)


if __name__ == "__main__":
    main()
