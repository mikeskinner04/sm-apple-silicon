#!/usr/bin/env python3
"""Replace the SM API's networked transport with a working Darwin implementation.

The macOS build of libsm_api ships a LinuxSockInterface whose receive path was
compiled out. All ten transport methods are virtual and contiguous in
__ZTV18LinuxSockInterface, so we overwrite the vtable slots at runtime and
supply our own. Nothing on disk is modified, so the code signature stays valid.

Usage:
    python3 sm_transport.py ./libsm_api.2.3.7.dylib 192.168.2.2 192.168.2.10 51665

The library asks for a 100 MB SO_RCVBUF and treats refusal as fatal. macOS caps
that at kern.ipc.maxsockbuf, pinned at 8 MB on this machine and refusing larger
values with ERANGE, so this transport negotiates down instead. No sysctl change
is needed, and at 103 MB/s nothing has been dropped at 8 MB.

This is a correctness prototype, not a fast one. Python per-datagram receive is
fine for opening the device and for heavily decimated IQ, and nowhere near
enough for native rate.
"""

import ctypes
import os
import struct
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sm_filters

# Learned from the binary. See notes at the bottom of this file.
CMD_BYTES = 0x800          # commands are fixed 2048-byte datagrams
HDR_BYTES = 8              # per-datagram header, stripped from the payload
PAYLOAD_BYTES = 8192       # per-datagram payload
MAX_MSGS = 256             # max datagrams per transfer
SLOTS = 32                 # transfer slots per interface
RCVBUF_WANTED = 100_000_000   # what the original asks for; macOS usually refuses
RCVTIMEO = 2.0                # seconds

VTABLE_SYM = "__ZTV18LinuxSockInterface"
ANCHOR_SYM = "_smGetAPIVersion"

# Byte offsets into the vtable of each method we replace.
SLOT_ALLOCATE = 0x20
SLOT_DEALLOCATE = 0x28
SLOT_BEGIN_CMD = 0x30
SLOT_FINISH_CMD = 0x38
SLOT_BEGIN_DATA = 0x40
SLOT_BEGIN_DATA_BUF = 0x48
SLOT_FINISH_DATA = 0x50
SLOT_DATA = 0x58
SLOT_TIMED_OUT = 0x60
SLOT_XFER_LEN = 0x68

FD_OFFSET = 0x10           # the original keeps its socket here; its dtor closes it
AUX_BYTES = 8192           # last datagram of each transfer is the aux block
MAX_LOGGED_COMMANDS = 400
MAX_LOGGED_AUX = 8

COUNTERS = ("calls", "datagrams", "payload_bytes", "nonzero_calls", "scanned_calls",
            "zero_bytes", "scanned_bytes", "gaps", "lost", "resets")


def got_slots(path, wanted):
    """Map imported symbol names to their __got slot file addresses.

    Uses the indirect symbol table rather than disassembling the stubs, so it
    survives version bumps without hard-coded addresses.
    """
    data = open(path, "rb").read()
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off, got, indoff, symoff, nsyms, stroff = 32, None, None, None, None, None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            nsects = struct.unpack_from("<I", data, off + 64)[0]
            so = off + 72
            for _ in range(nsects):
                name = data[so:so + 16].rstrip(b"\x00").decode()
                addr, size = struct.unpack_from("<QQ", data, so + 32)
                reserved1 = struct.unpack_from("<I", data, so + 68)[0]
                if name == "__got":
                    got = (addr, size, reserved1)
                so += 80
        elif cmd == 0x2:  # LC_SYMTAB
            _, _, symoff, nsyms, stroff, _ = struct.unpack_from("<IIIIII", data, off)
        elif cmd == 0xB:  # LC_DYSYMTAB
            indoff = struct.unpack_from("<20I", data, off)[14]
        off += cmdsize

    names = []
    for i in range(nsyms):
        n_strx = struct.unpack_from("<IBBHQ", data, symoff + i * 16)[0]
        end = data.index(b"\x00", stroff + n_strx)
        names.append(data[stroff + n_strx:end].decode())

    gaddr, gsize, reserved1 = got
    found = {}
    for i in range(gsize // 8):
        idx = struct.unpack_from("<I", data, indoff + (reserved1 + i) * 4)[0]
        if idx < len(names) and names[idx] in wanted:
            found[names[idx]] = gaddr + i * 8
    return found


def patch_semaphores(path, slide, shim_path):
    """Point the library's sem_* imports at the shim.

    Only libsm_api's own GOT is touched, so Python's semaphores are unaffected.
    """
    mapping = {"_sem_init": "shim_sem_init", "_sem_wait": "shim_sem_wait",
               "_sem_post": "shim_sem_post", "_sem_destroy": "shim_sem_destroy"}
    slots = got_slots(path, set(mapping))
    missing = set(mapping) - slots.keys()
    if missing:
        sys.exit(f"no __got slots for {sorted(missing)}")

    shim = ctypes.CDLL(shim_path)
    KEEP_ALIVE.append(shim)
    for imported, replacement in mapping.items():
        target = ctypes.cast(getattr(shim, replacement), ctypes.c_void_p).value
        ctypes.c_void_p.from_address(slots[imported] + slide).value = target
    print(f"redirected {len(mapping)} semaphore imports to {shim_path}")


def symbol_addresses(path, wanted):
    """Pull file addresses for named symbols straight out of LC_SYMTAB."""
    data = open(path, "rb").read()
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off, symoff, nsyms, stroff = 32, None, None, None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x2:  # LC_SYMTAB
            _, _, symoff, nsyms, stroff, _ = struct.unpack_from("<IIIIII", data, off)
        off += cmdsize
    found = {}
    for i in range(nsyms):
        n_strx, n_type, _, _, n_value = struct.unpack_from("<IBBHQ", data, symoff + i * 16)
        if not n_value:
            continue
        end = data.index(b"\x00", stroff + n_strx)
        name = data[stroff + n_strx:end].decode()
        if name in wanted:
            found[name] = n_value
    missing = wanted - found.keys()
    if missing:
        sys.exit(f"symbols not found in {path}: {sorted(missing)}")
    return found



class InterfaceStatus:
    """The library's per-device transfer status: read it, and clear it.

    DeviceInterface keeps one int that any failed command or data transfer sets
    to -6, smConnectionLostErr. Nothing on the networked path ever clears it,
    and the I/Q engine refuses to return samples while it is set, so a single
    failure anywhere, even an aux fetch behind a getter, ends the session.

    Located by symbol and decoded from the instructions that use it, not hard
    coded: `_deviceList` is the handle table, `SmDevice::UpdateAuxData` loads
    the interface with `ldr x0, [xN, #off]`, and
    `DeviceInterface::TransferStatus` is `ldr w0, [x0, #off]; ret`.
    """

    SYMS = {"_deviceList", "__ZN8SmDevice13UpdateAuxDataEb",
            "__ZNK15DeviceInterface14TransferStatusEv"}

    def __init__(self, path, slide):
        a = symbol_addresses(path, self.SYMS)
        self.table = a["_deviceList"] + slide
        self.iface_off = self._ldr_offset(a["__ZN8SmDevice13UpdateAuxDataEb"] + slide, 20, 8)
        self.status_off = self._ldr_offset(
            a["__ZNK15DeviceInterface14TransferStatusEv"] + slide, 1, 4)
        if self.iface_off is None or self.status_off is None:
            raise RuntimeError("could not decode the interface status offsets; "
                               "the library layout has changed")

    @staticmethod
    def _ldr_offset(addr, count, scale):
        """Offset of the first `ldr x0/w0, [xN, #imm]` among `count` instructions."""
        opcode = 0xF9400000 if scale == 8 else 0xB9400000
        for i in range(count):
            w = ctypes.c_uint32.from_address(addr + 4 * i).value
            if (w & 0xFFC00000) == opcode and (w & 31) == 0:
                return ((w >> 10) & 0xFFF) * scale
        return None

    def _field(self, dev):
        if not 0 <= dev < 16:
            return None
        device = ctypes.c_void_p.from_address(self.table + 8 * dev).value
        iface = device and ctypes.c_void_p.from_address(device + self.iface_off).value
        return iface + self.status_off if iface else None

    def read(self, dev):
        """The status for a handle: 0, -6, or None if there is no device."""
        f = self._field(dev)
        return None if f is None else ctypes.c_int32.from_address(f).value

    def clear(self, dev):
        """Set it back to 0, as the USB path does once at connect. Returns the
        value it had. If the device really has gone, the next transfer fails
        and sets it again."""
        f = self._field(dev)
        if f is None:
            return None
        old = ctypes.c_int32.from_address(f).value
        ctypes.c_int32.from_address(f).value = 0
        return old


def load_filter_tables(enabled=True):
    """Load filter_tables.json from beside this file, if repair is wanted.

    Produce it once with:  python3 sm_filters.py extract <path to a Linux .so>
    """
    if not enabled:
        print("filter repair disabled; I/Q filter uploads sent as the library built them")
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), sm_filters.TABLES_FILE)
    if not os.path.exists(path):
        print(f"no {sm_filters.TABLES_FILE}; I/Q filter uploads will not be repaired")
        return None
    tables = sm_filters.load_tables(path)
    print(f"filter repair on: shipped tables for {sorted(tables)} taps")
    return tables

class Transport:
    """Per-interface state, keyed by the C++ `this` pointer."""

    def __init__(self, filter_tables=None):
        self.by_this = {}
        # Signal Hound's shipped decimation filter coefficients, used to repair
        # the impulse uploads the macOS build sends. filter_tables is the live
        # setting: setting it to None disables repair, and callers may flip it
        # per experiment. shipped_tables keeps the loaded set regardless, so a
        # caller can tell "repair is off right now" from "no tables were ever
        # loaded".
        self.filter_tables = filter_tables
        self.shipped_tables = filter_tables

    def state(self, this):
        st = self.by_this.get(this)
        if st is None:
            buffers = [ctypes.create_string_buffer(MAX_MSGS * PAYLOAD_BYTES)
                       for _ in range(SLOTS)]
            st = {
                "sock": None,
                "buffers": buffers,
                "views": [memoryview(b).cast("B") for b in buffers],
                "requested": [0] * SLOTS,
                "timed_out": [False] * SLOTS,
                "cmd_sent": [0] * SLOTS,
                "headers": [],
                "last_seq": None,
                "gaps": 0,
                "lost": 0,
                "calls": 0,
                "datagrams": 0,
                "payload_bytes": 0,
                "nonzero_calls": 0,
                "scanned_calls": 0,
                "zero_bytes": 0,
                "scanned_bytes": 0,
                "raw_sample": None,
                "resets": 0,
                "commands": [],
                "aux_blocks": [],
                "filter_repairs": [],
                "req_seen": set(),
                "events": [],
            }
            self.by_this[this] = st
        return st

    @staticmethod
    def event(st, kind, **detail):
        """Record a transport failure so it reaches the report, not just stdout."""
        if len(st["events"]) < 50:
            st["events"].append({"kind": kind, "t": round(time.monotonic(), 3), **detail})

    def snapshot(self):
        """Counters and captured artefacts across all interfaces, then clear."""
        out = {c: 0 for c in COUNTERS}
        commands, aux, raw, repairs, events = [], [], None, [], []
        for st in self.by_this.values():
            for c in COUNTERS:
                out[c] += st[c]
                st[c] = 0
            out.setdefault("req_seen", set()).update(st["req_seen"])
            commands.extend(st["commands"])
            aux.extend(st["aux_blocks"])
            repairs.extend(st["filter_repairs"])
            st["filter_repairs"] = []
            events.extend(st["events"])
            st["events"] = []
            raw = raw or st["raw_sample"]
            st["commands"], st["aux_blocks"], st["raw_sample"] = [], [], None
            st["req_seen"] = set()
        out["req_seen"] = sorted(out.get("req_seen", set()))
        out["commands"] = commands
        out["aux_blocks"] = [a.hex() for a in aux]
        out["filter_repairs"] = repairs
        out["events"] = events
        out["raw_sample"] = raw
        return out

    # --- the ten replaced methods ---

    def allocate(self, this, host, dev, port):
        import socket
        st = self.state(this)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # macOS returns ENOBUFS rather than clamping, so walk down until it takes.
            size = RCVBUF_WANTED
            while size >= 1 << 20:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
                    break
                except OSError:
                    size //= 2
            got = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            print(f"  SO_RCVBUF {got} (asked {RCVBUF_WANTED})")
            s.settimeout(RCVTIMEO)
            s.bind((host.decode(), port))
            s.connect((dev.decode(), port))
        except OSError as e:
            print(f"  ! AllocateResources failed: {e}")
            s.close()
            return False
        st["sock"] = s
        ctypes.c_int.from_address(this + FD_OFFSET).value = s.fileno()
        print(f"  AllocateResources {host.decode()} -> {dev.decode()}:{port} ok (fd {s.fileno()})")
        return True

    def deallocate(self, this):
        st = self.state(this)
        if st["sock"]:
            st["sock"].close()
            st["sock"] = None
        ctypes.c_int.from_address(this + FD_OFFSET).value = -1

    def begin_cmd(self, this, idx, cmd_ptr):
        st = self.state(this)
        payload = ctypes.string_at(cmd_ptr, CMD_BYTES)
        if len(st["commands"]) < MAX_LOGGED_COMMANDS:
            # Commands are 32-bit words, opcode in the top byte. Trailing zeros
            # are padding to the fixed 2048-byte packet, so trim them. Logged
            # before any repair, so the log shows what the library produced.
            words = struct.unpack(f"<{CMD_BYTES // 4}I", payload)
            while words and words[-1] == 0:
                words = words[:-1]
            st["commands"].append(list(words))
        if self.filter_tables is not None:
            words = list(struct.unpack(f"<{CMD_BYTES // 4}I", payload))
            words, report = sm_filters.repair_packet(words, self.filter_tables)
            if any(r["impulse"] for r in report):
                payload = struct.pack(f"<{CMD_BYTES // 4}I",
                                      *[w & 0xFFFFFFFF for w in words])
                st["filter_repairs"].extend(r for r in report if r["impulse"])
        try:
            st["cmd_sent"][idx] = st["sock"].send(payload)
        except (OSError, AttributeError) as e:
            print(f"  ! send failed: {e}")
            st["cmd_sent"][idx] = 0
            self.event(st, "send_failed", slot=idx, error=str(e),
                       first_word=f"{struct.unpack_from('<I', payload)[0]:#010x}")

    def finish_cmd(self, this, idx):
        st = self.state(this)
        n, st["cmd_sent"][idx] = st["cmd_sent"][idx], 0
        return n

    def begin_data(self, this, idx, length, arg3):
        st = self.state(this)
        st["requested"][idx] = length
        st["timed_out"][idx] = False

    def begin_data_buf(self, this, idx, buf, length, arg3):
        self.begin_data(this, idx, length, arg3)

    def finish_data(self, this, idx):
        """Scatter each datagram: 8-byte header aside, 8192-byte payload inline."""
        st = self.state(this)
        want = st["requested"][idx]
        nmsgs = want // PAYLOAD_BYTES
        if nmsgs < 1:
            return 0

        mv = st["views"][idx]
        hdr = bytearray(HDR_BYTES)
        got = 0
        st["calls"] += 1
        if len(st["req_seen"]) < 8:
            st["req_seen"].add(want)
        if want > MAX_MSGS * PAYLOAD_BYTES:
            print(f"  ! transfer of {want} exceeds the {MAX_MSGS * PAYLOAD_BYTES} "
                  f"byte slot buffer")
        for i in range(nmsgs):
            view = mv[i * PAYLOAD_BYTES:(i + 1) * PAYLOAD_BYTES]
            try:
                nbytes, _, _, _ = st["sock"].recvmsg_into([hdr, view])
            except OSError as e:
                print(f"  ! recv timeout/error on slot {idx} after {i} msgs: {e}")
                st["timed_out"][idx] = True
                self.event(st, "recv_failed", slot=idx, requested=want,
                           datagrams_wanted=nmsgs, datagrams_got=i, error=str(e))
                break
            if len(st["headers"]) < 32:
                st["headers"].append(bytes(hdr))
            seq = int.from_bytes(hdr[:4], "little")
            last = st["last_seq"]
            if last is not None and seq != (last + 1) & 0xFFFFFFFF:
                if seq < last:
                    st["resets"] += 1        # counter restarted, not loss
                else:
                    st["gaps"] += 1
                    st["lost"] += seq - last - 1
            st["last_seq"] = seq
            st["datagrams"] += 1
            st["payload_bytes"] += max(0, nbytes - HDR_BYTES)
            if nbytes != HDR_BYTES + PAYLOAD_BYTES:
                print(f"  ! short datagram: {nbytes} bytes")
            got += max(0, nbytes - HDR_BYTES)

        # Every hundredth transfer, measure the whole thing rather than a corner
        # of it. bytes().count() runs at C speed, so this costs almost nothing.
        if got and st["calls"] % 100 == 0:
            blob = bytes(mv[:got])
            st["scanned_calls"] += 1
            st["zero_bytes"] += blob.count(0)
            st["scanned_bytes"] += len(blob)
            if any(blob):
                st["nonzero_calls"] += 1
            if st["raw_sample"] is None and want > 32768:
                st["raw_sample"] = blob
        # The engine reads AuxData from the last 8192 bytes of each transfer.
        # That block carries the device's own status, so keep a few.
        if got >= AUX_BYTES and want > 32768 and len(st["aux_blocks"]) < MAX_LOGGED_AUX:
            st["aux_blocks"].append(bytes(mv[got - AUX_BYTES:got][:256]))
        return got

    def data(self, this, idx):
        return ctypes.cast(self.state(this)["buffers"][idx], ctypes.c_void_p).value

    def timed_out(self, this, idx):
        return self.state(this)["timed_out"][idx]

    def xfer_len(self, this, idx):
        return self.state(this)["requested"][idx]


# Signatures. `this` arrives in x0 like any other pointer argument.
SIGS = {
    SLOT_ALLOCATE: (ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_char_p,
                                     ctypes.c_char_p, ctypes.c_uint16), "allocate"),
    SLOT_DEALLOCATE: (ctypes.CFUNCTYPE(None, ctypes.c_void_p), "deallocate"),
    SLOT_BEGIN_CMD: (ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_void_p), "begin_cmd"),
    SLOT_FINISH_CMD: (ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_int), "finish_cmd"),
    SLOT_BEGIN_DATA: (ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int), "begin_data"),
    SLOT_BEGIN_DATA_BUF: (ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_int), "begin_data_buf"),
    SLOT_FINISH_DATA: (ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                        ctypes.c_int), "finish_data"),
    SLOT_DATA: (ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_int), "data"),
    SLOT_TIMED_OUT: (ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                      ctypes.c_int), "timed_out"),
    SLOT_XFER_LEN: (ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_int), "xfer_len"),
}

KEEP_ALIVE = []  # callbacks must outlive the patch


def patch_vtable(vtable_addr, transport):
    libc = ctypes.CDLL(None)
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    page = 16384
    base = vtable_addr & ~(page - 1)
    if libc.mprotect(ctypes.c_void_p(base), page * 2, 1 | 2) != 0:
        sys.exit("mprotect failed; cannot make the vtable writable")

    for slot, (proto, method) in SIGS.items():
        cb = proto(getattr(transport, method))
        KEEP_ALIVE.append(cb)
        ctypes.c_void_p.from_address(vtable_addr + slot).value = ctypes.cast(
            cb, ctypes.c_void_p).value
    print(f"patched {len(SIGS)} vtable slots at {vtable_addr:#x}")


def main():
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    path, host_ip, dev_ip, port = sys.argv[1:5]

    addrs = symbol_addresses(path, {VTABLE_SYM, ANCHOR_SYM})
    lib = ctypes.CDLL(path)

    lib.smGetAPIVersion.restype = ctypes.c_char_p
    lib.smGetErrorString.restype = ctypes.c_char_p
    lib.smGetErrorString.argtypes = [ctypes.c_int]
    lib.smOpenNetworkedDevice.restype = ctypes.c_int
    lib.smOpenNetworkedDevice.argtypes = [ctypes.POINTER(ctypes.c_int),
                                          ctypes.c_char_p, ctypes.c_char_p,
                                          ctypes.c_uint16]

    runtime = ctypes.cast(lib.smGetAPIVersion, ctypes.c_void_p).value
    slide = runtime - addrs[ANCHOR_SYM]
    print(f"api {lib.smGetAPIVersion().decode()}  slide {slide:#x}")

    transport = Transport(load_filter_tables())
    patch_vtable(addrs[VTABLE_SYM] + slide, transport)

    shim_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sem_shim.dylib")
    if os.path.exists(shim_path):
        patch_semaphores(path, slide, shim_path)
    else:
        print("no sem_shim.dylib alongside this script; engines will not work")

    handle = ctypes.c_int(-1)
    status = lib.smOpenNetworkedDevice(ctypes.byref(handle), host_ip.encode(),
                                       dev_ip.encode(), int(port))
    print(f"\nsmOpenNetworkedDevice -> {status} "
          f"({lib.smGetErrorString(status).decode()})  handle {handle.value}")

    for st in transport.by_this.values():
        if st["headers"]:
            print("\nfirst datagram headers:")
            for h in st["headers"][:16]:
                print("  ", h.hex(" "))

    if status >= 0:
        lib.smCloseDevice(handle)


if __name__ == "__main__":
    main()

# Notes, all read out of libsm_api.2.3.7.dylib:
#
#   AllocateResources: socket(AF_INET, SOCK_DGRAM), SO_RCVTIMEO 2s, SO_REUSEADDR,
#   SO_RCVBUF 100,000,000, bind(hostIP:port), connect(deviceIP:port). Same port
#   both ends. Then 32 slots each get an AlignedMalloc of 256 * 8192 = 2 MB.
#
#   Commands: send() of exactly 0x800 bytes.
#
#   Data: the original builds 256 msghdrs, each with two iovecs, iov[0] an
#   8-byte header into a side array and iov[1] 8192 bytes into the slot buffer.
#   BeginDataXfer stores len and computes len / 8192 as the datagram count.
#
#   DeviceInterfaceNetworked::FinishDataXfer returns {timedOut, xferLenRequested,
#   bytesReceived, dataPtr} and sets device status -6 if timedOut or if
#   received != requested.
#
#   The msghdr layout in the binary is Linux-shaped (56 bytes, size_t msg_iovlen)
#   rather than Darwin's 48, which is further evidence this was never adapted.
