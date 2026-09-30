#!/usr/bin/env python3
"""Offline test of the Python transport against the sm_sim device simulator.

The same framing cases as test_native.py, for the backend the diagnostics
harness uses: a counter wrap, surplus datagrams that slip the transfer
boundaries, and unrequested datagrams arriving between two streams.

    make && python3 test_transport.py
"""
import ctypes, os, struct, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import sm_transport as T

SIM = os.path.join(HERE, "sm_sim")
HOST, DEV, PORT, P = "127.0.0.1", "127.0.0.2", 51710, 8192

def sim(**o):
    args = [SIM, DEV, str(PORT), "--pace-us", "100"]
    for k, v in o.items(): args += [f"--{k.replace('_','-')}", str(v)]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
    assert "ready" in p.stdout.readline(); return p

def stop(p):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(struct.pack("<I", 0x7E570002) + b"\0" * 2044, (DEV, PORT)); s.close()
    out = p.stdout.readline().strip(); p.wait(5); return out

def run(tr, this, n, count, depth=2):
    cmdbuf = ctypes.create_string_buffer(2048)
    def req(slot):
        tr.begin_data(this, slot, n * P, 2000)
        ctypes.memmove(cmdbuf, struct.pack("<II", 0x7E570001, n) + b"\0" * 2040, 2048)
        tr.begin_cmd(this, slot, ctypes.addressof(cmdbuf))
    for k in range(min(depth, count)): req(k)
    out = []
    for k in range(count):
        slot = k % depth
        got = tr.finish_data(this, slot)
        raw = bytes(tr.state(this)["views"][slot][:n * P])
        pos = []
        for j in range(n):
            w1 = struct.unpack_from("<I", raw, j * P + 4)[0]
            pos.append("S" if w1 == 0xFFFFFFF0 else w1 - 1)
        aux = raw[(n - 1) * P:(n - 1) * P + 4] == b"4XU1"
        out.append((got, pos, aux))
        if k + depth < count: req(slot)
    return out

def exact(res, n, start=0, skip=()):
    return all(p == start + k * n + j for k, (_, pos, _) in enumerate(res) if k not in skip
               for j, p in enumerate(pos))

def case(name, n, count, check, **o):
    p = sim(**o)
    tr = T.Transport(None)
    obj = ctypes.create_string_buffer(0x400); this = ctypes.addressof(obj)
    assert tr.allocate(this, HOST.encode(), DEV.encode(), PORT)
    try:
        res = run(tr, this, n, count)
        st = tr.state(this)
        ok, detail = check(res, st)
    finally:
        tr.deallocate(this); summary = stop(p)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}  [{summary}]")
    return ok

ok = True
ok &= case("clean with wrap", 32, 20, lambda r, st: (
    exact(r, 32) and all(a for *_, a in r) and st["aux_misframed"] == 0 and st["resets"] == 0,
    f"exact, aux in place, resets {st['resets']} (16-bit wrap not miscounted)"), seq_start=0xFFF0)
ok &= case("surplus 1 resyncs", 32, 20, lambda r, st: (
    exact(r, 32, skip={2}) and all(a for *_, a in r[3:]) and st["resyncs"] == 1
    and st["aux_misframed"] == 1,
    f"resyncs {st['resyncs']}, misframed {st['aux_misframed']}, transfers 3 on exact"),
    surplus_after=80)
ok &= case("surplus 3 resyncs", 32, 20, lambda r, st: (
    exact(r, 32, skip={2}) and all(a for *_, a in r[3:]) and st["resync_dropped"] == 3,
    f"resyncs {st['resyncs']}, dropped {st['resync_dropped']}"),
    surplus_after=80, surplus_count=3)

# stale between streams
p = sim(); tr = T.Transport(None)
obj = ctypes.create_string_buffer(0x400); this = ctypes.addressof(obj)
assert tr.allocate(this, HOST.encode(), DEV.encode(), PORT)
first = run(tr, this, 32, 4)
cb = ctypes.create_string_buffer(struct.pack("<II", 0x7E570003, 5) + b"\0" * 2040, 2048)
tr.begin_cmd(this, 30, ctypes.addressof(cb)); time.sleep(0.3)
second = run(tr, this, 32, 4)
st = tr.state(this); tr.deallocate(this); summary = stop(p)
good = exact(first, 32) and exact(second, 32, start=128) and st["stale_flushed"] == 5 \
    and st["lost"] == 0 and all(a for *_, a in second)
print(f"  {'ok  ' if good else 'FAIL'} stale flushed at idle: flushed {st['stale_flushed']}, "
      f"lost {st['lost']}, second stream exact  [{summary}]")
ok &= good
print("\npython transport:", "all tests passed" if ok else "FAILED")
sys.exit(0 if ok else 1)
