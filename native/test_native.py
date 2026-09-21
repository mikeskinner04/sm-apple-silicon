#!/usr/bin/env python3
"""Offline test of the native transport against the sm_sim device simulator.

No dylib and no hardware. It fakes the C++ object, then drives the transport
the way EngineIQStreamingNetworked::XferThreadDirect does: several transfers
armed at once, each followed by its request command, then finish, check and
re-arm in turn. Every datagram carries its true stream position, so the checks
are exact: each one must land in the slot and offset it belongs to, and each
lost one must leave a zero-filled hole in exactly its place.

    make && python3 test_native.py
"""

import ctypes
import os
import socket
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from sm_native import Stats  # noqa: E402  one definition of the struct

PAYLOAD = 8192
# Host and simulated device need different addresses on loopback, since both
# bind the same port. On the real link they are different machines anyway.
HOST_ADDR, DEV_ADDR, PORT = "127.0.0.1", "127.0.0.2", 51700


def load():
    for name in ("libsmnative.so", "libsmnative.dylib"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            lib = ctypes.CDLL(p)
            break
    else:
        sys.exit("build first: make")
    v, i, c = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    lib.smn_allocate.restype = ctypes.c_bool
    lib.smn_allocate.argtypes = [v, c, c, ctypes.c_uint16]
    lib.smn_deallocate.argtypes = [v]
    lib.smn_begin_data.argtypes = [v, i, i, i]
    lib.smn_begin_cmd.argtypes = [v, i, v]
    lib.smn_finish_data.argtypes = [v, i]
    lib.smn_finish_data.restype = i
    lib.smn_data.argtypes = [v, i]
    lib.smn_data.restype = v
    lib.smn_timed_out.argtypes = [v, i]
    lib.smn_timed_out.restype = ctypes.c_bool
    lib.smn_repair_packet.argtypes = [v, i]
    lib.smn_repair_packet.restype = i
    lib.smn_set_filter_table.argtypes = [i, ctypes.POINTER(ctypes.c_double)]
    lib.smn_set_timeout_ms.argtypes = [i]
    lib.smn_stats_size.restype = i
    lib.smn_get_stats.argtypes = [v, ctypes.POINTER(Stats)]
    return lib


def stats(lib, owner):
    s = Stats()
    lib.smn_get_stats(owner, ctypes.byref(s))
    return s.as_dict()


class Sim:
    def __init__(self, **opts):
        # The device sends at the rate it samples, not in instant 2 MB bursts.
        # Pacing the simulator the same way keeps the test about placement
        # rather than about how fast one loopback socket drains.
        opts.setdefault("pace_us", 100)
        args = [os.path.join(HERE, "sm_sim"), DEV_ADDR, str(PORT)]
        for k, val in opts.items():
            args += [f"--{k.replace('_', '-')}", str(val)]
        self.p = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
        line = self.p.stdout.readline()
        assert "ready" in line, (
            f"simulator did not start on {DEV_ADDR}; on macOS add the address "
            f"first with: sudo ifconfig lo0 alias {DEV_ADDR} up")
        self.summary = ""

    def stop(self):
        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.sendto(struct.pack("<I", 0x7E570002) + b"\0" * 2044, (DEV_ADDR, PORT))
        so.close()
        self.summary = self.p.stdout.readline().strip()
        self.p.wait(timeout=5)


class Owner:
    """Stands in for the C++ LinuxSockInterface: the transport writes the
    socket fd at +0x10 and the error byte at +0x08."""

    def __init__(self, lib):
        self.lib = lib
        self.buf = ctypes.create_string_buffer(0x400)
        self.ptr = ctypes.addressof(self.buf)
        assert lib.smn_allocate(self.ptr, HOST_ADDR.encode(), DEV_ADDR.encode(), PORT)

    def arm_and_request(self, slot, n):
        self.lib.smn_begin_data(self.ptr, slot, n * PAYLOAD, 2000)
        cmd = ctypes.create_string_buffer(
            struct.pack("<II", 0x7E570001, n) + b"\0" * 2040, 2048)
        self.lib.smn_begin_cmd(self.ptr, slot, cmd)

    def finish(self, slot, n):
        got = self.lib.smn_finish_data(self.ptr, slot)
        to = self.lib.smn_timed_out(self.ptr, slot)
        raw = ctypes.string_at(self.lib.smn_data(self.ptr, slot), n * PAYLOAD)
        pos = []
        for j in range(n):
            w0, inv = struct.unpack_from("<I", raw, j * PAYLOAD)[0], \
                struct.unpack_from("<I", raw, j * PAYLOAD + PAYLOAD - 4)[0]
            if w0 == 0 and inv == 0:
                pos.append(None)                     # zero-filled hole
            else:
                assert w0 ^ inv == 0xFFFFFFFF, "corrupt payload"
                pos.append(w0 - 1)                   # true stream position
        return got, to, pos

    def close(self):
        self.lib.smn_deallocate(self.ptr)


def stream(owner, n, count, depth=8):
    """XferThreadDirect in miniature. Returns [(bytes, timed_out, positions)].

    Keep what is in flight to a third of the socket buffer, since kernel
    accounting overhead eats into it: 2 transfers with a stock 8 MB buffer.
    """
    rcvbuf = stats(owner.lib, owner.ptr)["rcvbuf"]
    depth = max(2, min(depth, rcvbuf // (3 * n * PAYLOAD)))
    for k in range(min(depth, count)):
        owner.arm_and_request(k, n)
    out = []
    for k in range(count):
        slot = k % depth
        out.append(owner.finish(slot, n))
        if k + depth < count:
            owner.arm_and_request(slot, n)
    return out


def exact(results, n, dropped=lambda p: False):
    """Every slot must hold position base+j, or a hole exactly where the
    simulator dropped. Returns (ok, holes)."""
    holes = 0
    for k, (_, _, pos) in enumerate(results):
        for j, p in enumerate(pos):
            want = k * n + j
            if p is None:
                if not dropped(want):
                    return False, holes
                holes += 1
            elif p != want:
                return False, holes
    return True, holes


def report(name, ok, detail):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    return 0 if ok else 1


def test_filter_repair(lib):
    fails = 0
    for n, scale, addr in [(189, 1 << 19, 0x478), (79, 1 << 18, 0x578),
                           (159, 1 << 18, 0x678), (159, 1 << 18, 0x778)]:
        coeffs = (ctypes.c_double * n)(*[1.0 + (i == n // 2) for i in range(n)])
        lib.smn_set_filter_table(n, coeffs)
        half = (n + 1) // 2
        words = [0] * 512
        words[0] = (0x04 << 24) | ((half + 8) << 16) | addr
        words[9 + half - 1] = scale
        buf = (ctypes.c_uint32 * 512)(*words)
        reps = lib.smn_repair_packet(buf, 512)
        centre = buf[9 + half - 1]
        again = lib.smn_repair_packet(buf, 512)      # must leave real taps alone
        fails += report(f"filter stage {addr:#05x}", reps == 1 and again == 0
                        and 0 < centre < scale, f"centre {scale} -> {centre}")
    return fails


def run_case(lib, name, n, count, check, **sim_opts):
    sim = Sim(**sim_opts)
    owner = Owner(lib)
    try:
        results = stream(owner, n, count)
        st = stats(lib, owner.ptr)
    finally:
        owner.close()
        sim.stop()
    ok, detail = check(results, st)
    return report(name, ok, f"{detail}  [{sim.summary}]")


def main():
    lib = load()
    assert lib.smn_stats_size() == ctypes.sizeof(Stats), "stats struct mismatch"
    lib.smn_set_timeout_ms(400)
    full = 256 * PAYLOAD
    fails = test_filter_repair(lib)

    # 16-bit wrap in the middle of the first transfer, then clean streaming
    def clean(r, st):
        ok, holes = exact(r, 256)
        good = ok and holes == 0 and st["lost"] == 0 and st["resets"] == 0 \
            and all(g == full and not t for g, t, _ in r)
        return good, f"{len(r)} transfers exact across the wrap, lost {st['lost']}"
    fails += run_case(lib, "clean, counter wraps mid-transfer", 256, 48, clean,
                      seq_start=0xFF80)

    # loss: every 97th dropped, landing anywhere including the aux datagram
    def loss(r, st):
        ok, holes = exact(r, 256, dropped=lambda p: p % 97 == 96)
        aux = sum(1 for p in range(256 * len(r)) if p % 97 == 96 and p % 256 == 255)
        good = ok and holes == st["lost"] > 0 and st["aux_lost"] == aux \
            and st["timeouts"] == 0 and all(g == full and not t for g, t, _ in r)
        return good, (f"lost {st['lost']} as {holes} holes in place, aux lost "
                      f"{st['aux_lost']}, every transfer reported complete")
    fails += run_case(lib, "loss zero-filled in place", 256, 48, loss,
                      seq_start=0xFFF0, drop_every=97)

    # every 128th dropped: half the losses are the aux datagram at the end
    def aux(r, st):
        ok, holes = exact(r, 256, dropped=lambda p: p % 128 == 127)
        return ok and st["aux_lost"] == len(r) and all(g == full for g, _, _ in r), \
            f"aux lost {st['aux_lost']} of {len(r)}, placement exact"
    fails += run_case(lib, "aux datagram lost", 256, 16, aux, drop_every=128)

    # duplicates must be dropped, not placed
    def dups(r, st):
        ok, holes = exact(r, 256)
        return ok and holes == 0 and st["strays"] > 0 and st["resets"] == 0, \
            f"strays dropped {st['strays']}, placement exact"
    fails += run_case(lib, "duplicates", 256, 24, dups, dup_every=50)

    # counter restart far away: placement falls back to arrival order, still exact
    def far(r, st):
        ok, holes = exact(r, 256)
        return ok and holes == 0 and st["resets"] == 1 and st["lost"] == 0, \
            f"resets {st['resets']}, lost {st['lost']}, placement exact"
    fails += run_case(lib, "counter restart, far", 256, 16, far,
                      seq_start=0x4000, reset_after=300)

    # counter restart close behind: the run of stale-looking datagrams before
    # the resync becomes a hole, and everything after keeps its true place.
    def near(r, st):
        ok, holes = exact(r, 256, dropped=lambda p: 300 <= p < 307)
        return ok and holes == 7 and st["resets"] == 1 and st["lost"] == 7 \
            and st["strays"] == 0 and st["timeouts"] == 0, \
            f"resets {st['resets']}, lost {st['lost']} as holes, alignment kept"
    fails += run_case(lib, "counter restart, near", 256, 16, near,
                      seq_start=0x0100, reset_after=300)

    # small (calibration-sized) transfers keep strict reporting
    def strict(r, st):
        ok, holes = exact(r, 4, dropped=lambda p: p % 3 == 2)
        honest = all(g == sum(p is not None for p in pos) * PAYLOAD for g, _, pos in r)
        shorts = sum(g < 4 * PAYLOAD for g, _, _ in r)
        return ok and honest and shorts == len(r), \
            f"{shorts} of {len(r)} damaged reads reported short, as the open expects"
    fails += run_case(lib, "small transfers stay strict", 4, 6, strict,
                      drop_every=3)

    # silence on a stream transfer: reported as a timeout
    sim = Sim()
    owner = Owner(lib)
    lib.smn_begin_data(owner.ptr, 0, full, 2000)     # armed, never requested
    got = lib.smn_finish_data(owner.ptr, 0)
    to = lib.smn_timed_out(owner.ptr, 0)
    owner.close()
    sim.stop()
    fails += report("silence times out", got == 0 and to, f"bytes {got}, timed out {to}")

    print("\n" + ("all tests passed" if fails == 0 else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
