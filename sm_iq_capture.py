#!/usr/bin/env python3
"""Capture I/Q from an SM200C on macOS through the patched transport.

Reuses sm_transport.py for the vtable and semaphore patches, then drives the
normal SM API: configure, smConfigure(smModeIQStreaming), smGetIQ in a loop.
Signatures are taken from sm_api.h, not guessed.

    python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib \
        --host 192.168.2.2 --device 192.168.2.10 --port 51665 \
        --center 1e9 --decimation 64 --seconds 2

Start with a high decimation. The kernel caps our socket receive buffer at
8 MB against the 32 MB the device can keep in flight, and this transport
receives datagram by datagram in Python, so low decimations will drop data.
"""

import argparse
import ctypes
import os
import sys
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
    ap.add_argument("--out", default="capture.iq",
                    help="single frequency writes here; several get a -<MHz> suffix")
    args = ap.parse_args()
    centers = [float(c) for c in args.center.split(",")]

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
    if args.queue_ms is not None:
        check(lib, lib.smSetIQQueueSize(dev, args.queue_ms), "smSetIQQueueSize")

    total_losses = 0
    for n_freq, center in enumerate(centers):
        # Retuning means setting the frequency and reconfiguring. smConfigure
        # calls smAbort internally, so the engine thread is torn down and
        # rebuilt on every hop.
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

        block = 32768
        total = int(rate.value * args.seconds)
        buf = ctypes.create_string_buffer(block * bytes_per_sample)
        ns = ctypes.c_int64()
        loss, remaining = ctypes.c_int(), ctypes.c_int()

        captured, losses = 0, 0
        started = time.monotonic()
        with open(out, "wb") as f:
            first = True
            while captured < total:
                n = min(block, total - captured)
                status = lib.smGetIQ(dev, buf, n, None, 0, ctypes.byref(ns),
                                     smTrue if first else smFalse,
                                     ctypes.byref(loss), ctypes.byref(remaining))
                if status < 0:
                    print(f"  smGetIQ: {status} "
                          f"({lib.smGetErrorString(status).decode()})")
                    break
                if loss.value:
                    losses += 1
                f.write(buf.raw[:n * bytes_per_sample])
                captured += n
                first = False
        elapsed = time.monotonic() - started
        total_losses += losses

        print(f"  captured {captured} samples in {elapsed:.2f} s "
              f"({captured / elapsed / 1e6:.3f} MS/s sustained), "
              f"loss flags {losses} -> {out}")

    print(f"\ntotal sample-loss flags: {total_losses}")
    for st in transport.by_this.values():
        print(f"datagram gaps: {st['gaps']}, lost: {st['lost']}, "
              f"counter resets: {st['resets']}")
        print(f"FinishDataXfer calls: {st['calls']}, datagrams: {st['datagrams']}, "
              f"payload: {st['payload_bytes'] / 1e6:.2f} MB")
        if st["scanned_bytes"]:
            pct = 100.0 * st["zero_bytes"] / st["scanned_bytes"]
            print(f"full scans: {st['nonzero_calls']} of {st['scanned_calls']} "
                  f"transfers had any non-zero byte; {pct:.3f}% of scanned bytes zero")
        print(f"requested transfer sizes seen: {sorted(st['req_seen'])}")
        if st["raw_sample"]:
            with open("raw_transfer.bin", "wb") as f:
                f.write(st["raw_sample"])
            print(f"wrote raw_transfer.bin ({len(st['raw_sample'])} bytes, "
                  f"one untouched transfer straight off the wire)")
    print(f"wrote {args.out} "
          f"({'16-bit complex short' if args.short else '32-bit complex float'})")

    lib.smAbort(dev)
    lib.smCloseDevice(dev)


if __name__ == "__main__":
    main()
