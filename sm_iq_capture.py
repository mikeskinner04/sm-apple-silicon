#!/usr/bin/env python3
"""Capture I/Q from an SM200C on macOS.

A thin command line over sm200c.SM200C: open, configure I/Q streaming, read
blocks with a writer thread behind, and check the result. The native backend
is the default and handles decimation 1 (200 MS/s).

    python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --center 1e9 --decimation 64 --seconds 2
    python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --decimation 1 --short --seconds 10 --discard

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
from sm200c import SM200C, SmError, DEFAULT_HOST, DEFAULT_DEVICE, DEFAULT_PORT  # noqa: E402

SAMPLES_PER_DATAGRAM = 2048    # 8192-byte payload of 16-bit complex


def describe(sm):
    i = sm.info
    print(f"device   {i.model} serial {i.serial} firmware {i.firmware}")
    try:
        d = sm.diagnostics()
        print(f"health   {d['voltage']:.2f} V  {d['current']:.2f} A  {d['temperature']:.1f} C")
    except SmError:
        pass
    try:
        f = sm.sfp_diagnostics()
        print(f"sfp      {f['temperature']:.1f} C  {f['voltage']:.2f} V  "
              f"tx {f['tx_power_mW']:.3f} mW  rx {f['rx_power_mW']:.3f} mW")
    except SmError:
        pass


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


def capture(sm, out, total, discard):
    """Read `total` samples of the active stream and write them to `out`.

    A writer thread does the file I/O so the reading thread only ever waits on
    the library. Both release the GIL: ctypes drops it around smGetIQ and
    file.write drops it around the system call. Blocks are about 5 ms, so at
    200 MS/s that is 200 calls a second rather than 6,000.
    """
    raise_thread_qos()
    rate = sm.iq_info.sample_rate
    block = max(32768, min(1 << 20, int(rate * 0.005)))
    free, full = queue.Queue(), queue.Queue()
    for _ in range(8):
        free.put(sm.iq_buffer(block))
    failure = []

    def writer():
        with open(os.devnull if discard else out, "wb") as f:
            while True:
                item = full.get()
                if item is None:
                    return
                buf, n = item
                try:
                    if not discard:
                        f.write(buf[:n])
                except OSError as e:
                    failure.append(e)
                free.put(buf)

    w = threading.Thread(target=writer, name="iq-writer")
    w.start()
    first_ns = None
    captured = flags = max_backlog = sync_flags = 0
    started = time.monotonic()
    try:
        while captured < total and not failure:
            n = min(block, total - captured)
            buf = free.get()
            try:
                b = sm.read_iq(n, out=buf, purge=captured == 0)
            except SmError as e:
                free.put(buf)
                print(f"  {e}")
                break
            # A sync error still delivered the samples: keep them and count it.
            sync_flags += b.sync_error
            if first_ns is None:
                first_ns = b.timestamp_ns
            flags += b.sample_loss
            max_backlog = max(max_backlog, b.remaining)
            full.put((buf, n))
            captured += n
    finally:
        full.put(None)
        w.join()
    if failure:
        print(f"  write failed: {failure[0]}")
    return {"samples": captured, "seconds": time.monotonic() - started,
            "first_sample_ns": first_ns, "library_loss_flags": flags,
            "sync_flags": sync_flags,
            "max_backlog_ms": 1e3 * max_backlog / rate}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dylib")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--center", default="1e9",
                    help="centre frequency in Hz; comma-separated to retune between captures")
    ap.add_argument("--decimation", type=int, default=64, help="power of two")
    ap.add_argument("--ref-level", type=float, default=-20.0, help="dBm")
    ap.add_argument("--seconds", type=float, default=2.0, help="dwell per frequency")
    ap.add_argument("--queue-ms", type=float, default=None,
                    help="smSetIQQueueSize; smaller means faster retunes")
    ap.add_argument("--short", action="store_true", help="16-bit complex instead of 32-bit float")
    ap.add_argument("--filter-repair", choices=("design", "tables", "off"),
                    default="design",
                    help="what replaces the impulse filter uploads (default design)")
    ap.add_argument("--no-filter-repair", action="store_true",
                    help="same as --filter-repair off")
    ap.add_argument("--backend", choices=("native", "python"), default="native",
                    help="transport backend (default native; python cannot keep up "
                         "below decimation 8)")
    ap.add_argument("--native", action="store_true", help=argparse.SUPPRESS)  # old flag
    ap.add_argument("--discard", action="store_true",
                    help="read the stream but write nothing, to test the link without the disk")
    ap.add_argument("--no-promote", action="store_true",
                    help="native only: leave the library's thread priorities alone (A/B runs)")
    ap.add_argument("--out", default="capture.iq",
                    help="single frequency writes here; several get a -<MHz> suffix")
    args = ap.parse_args()
    centers = [float(c) for c in args.center.split(",")]
    if args.no_filter_repair:
        args.filter_repair = "off"
    native = args.backend == "native"

    with SM200C(args.dylib, args.host, args.device, args.port, backend=args.backend,
                filter_repair=args.filter_repair, promote=not args.no_promote) as sm:
        print()
        describe(sm)
        repair = sm.filter_repair
        sm.ref_level = args.ref_level
        hw_dec = min(args.decimation, 8)
        min_hole = max(64, SAMPLES_PER_DATAGRAM * hw_dec // args.decimation)

        for center in centers:
            # Retuning means reconfiguring; smConfigure tears the engine thread
            # down and rebuilds it on every hop. One failed transfer anywhere
            # leaves the library's connection-lost status at -6 for good, and
            # smGetIQ then refuses every call, so start each capture clean.
            status_in = sm.clear_connection_status()
            if status_in:
                print(f"  connection-lost status was stuck at {status_in} from an earlier "
                      f"failed transfer; cleared")
            tune_start = time.monotonic()
            repairs_before = repair.seen
            iq = sm.configure_iq(center=center, decimation=args.decimation,
                                 data_type="int16" if args.short else "complex64",
                                 queue_ms=args.queue_ms)
            tune_ms = (time.monotonic() - tune_start) * 1e3
            print(f"\ntuned to {iq.center/1e6:.6f} MHz in {tune_ms:.1f} ms")
            print(f"  {iq.sample_rate/1e6:.4f} MS/s, {iq.bandwidth/1e6:.3f} MHz bandwidth, "
                  f"correction {iq.correction:.6g}")
            new = min(repair.seen - repairs_before, len(repair.log))
            uploads = repair.log[-new:] if new else []
            if uploads:
                done = [r for r in uploads if r.get("repaired")]
                if done:
                    print(f"  filters ({repair.mode}): " + ", ".join(
                        f"stage {r['stage']} {r['fc']:.6f}" if "fc" in r else f"stage {r['stage']}"
                        for r in done))
                if len(done) < len(uploads):
                    print(f"  ! {len(uploads) - len(done)} impulse filter uploads not repaired: "
                          f"{sorted({r.get('reason', '?') for r in uploads if not r.get('repaired')})}")

            if len(centers) == 1:
                out = args.out
            else:
                stem, ext = os.path.splitext(args.out)
                out = f"{stem}-{iq.center/1e6:.3f}MHz{ext}"

            before = sm.transport_counters()
            result = capture(sm, out, int(iq.sample_rate * args.seconds), args.discard)
            after = sm.transport_counters()
            delta = {k: after[k] - before.get(k, 0) for k in after
                     if isinstance(after[k], int) and k not in
                     ("rcvbuf", "rx_sched", "max_outstanding") and not k.startswith("lib_")}

            captured, elapsed = result["samples"], result["seconds"]
            print(f"  captured {captured} samples in {elapsed:.2f} s "
                  f"({captured / elapsed / 1e6:.3f} MS/s sustained)"
                  f"{'' if args.discard else ' -> ' + out}")
            print(f"  library backlog peaked at {result['max_backlog_ms']:.1f} ms of its 500 ms")

            holes = None
            if native and not args.discard and delta.get("lost", 0):
                holes = find_holes(out, args.short, min_hole)

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
            if delta.get("aux_misframed") or delta.get("resyncs"):
                problems.append(f"{delta.get('aux_misframed', 0)} transfers out of frame, "
                                f"{delta.get('resyncs', 0)} resynchronised: extra datagrams "
                                f"arrived; about one transfer of samples is suspect per event")
            if result["sync_flags"]:
                problems.append(f"{result['sync_flags']} reads flagged a sync error "
                                f"(samples kept)")
            status_out = sm.connection_status
            if status_out:
                problems.append(f"connection-lost status set to {status_out} during the "
                                f"capture: a transfer or command failed")
            if holes:
                span = sum(h[1] for h in holes)
                problems.append(f"{len(holes)} zero-filled holes, {span} samples, "
                                f"positions in the sidecar")
            if delta.get("stale_flushed"):
                print(f"  note: {delta['stale_flushed']} unrequested datagrams were discarded "
                      f"before the stream started")
            print("  clean: no loss anywhere" if not problems else
                  "  DATA LOSS:\n    " + "\n    ".join(problems))

            if not args.discard:
                meta = {
                    "file": os.path.basename(out),
                    "datatype": "ci16_le" if args.short else "cf32_le",
                    "sample_rate": iq.sample_rate, "bandwidth": iq.bandwidth,
                    "center_hz": iq.center, "decimation": args.decimation,
                    "iq_correction": iq.correction,
                    "filter_repair_mode": repair.mode,
                    "filter_repairs": [r for r in repair.log if r.get("repaired")][-4:],
                    "backend": sm.backend,
                    "note": "16-bit samples need multiplying by iq_correction" if args.short else "",
                    **result,
                    "transport": delta,
                    "status_in": status_in, "status_out": status_out,
                    "holes": holes,
                }
                with open(out + ".json", "w") as f:
                    json.dump(meta, f, indent=1)

        s = sm.transport_counters()
        if native:
            import sm_native as N
            print(f"\nsession: {s['transfers']} transfers, {s['datagrams']} datagrams, "
                  f"{s['payload_bytes'] / 1e6:.1f} MB, lost {s['lost']}, timeouts {s['timeouts']}, "
                  f"filter repairs {s['filter_repairs']}")
            print(f"socket buffer {s['rcvbuf'] / 2**20:.0f} MB, receiver thread "
                  f"{N.RX_SCHED.get(s['rx_sched'], s['rx_sched'])}, deepest queue "
                  f"{s['max_outstanding']}, queue ran dry {s['queue_empty']} times")
            if s["lib_prio_low"]:
                policy = {1: "timeshare", 2: "round robin", 4: "fixed (FIFO)"}.get(
                    s["lib_policy_low"], str(s["lib_policy_low"]))
                print(f"library thread seen at {policy} priority {s['lib_prio_low']}; "
                      + (f"moved to priority {s['lib_prio_after']}" if s["lib_promotions"]
                         else "left alone (--no-promote)"))
        else:
            print(f"\nsession: {s['datagrams']} datagrams, gaps {s['gaps']}, "
                  f"lost {s['lost']}, counter resets {s['resets']}")


if __name__ == "__main__":
    main()
