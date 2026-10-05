# Guide

Getting an SM200C working on macOS, start to finish.

## 1. What you need

- An Apple Silicon Mac running macOS 12 or later.
- A Thunderbolt 10GbE adapter, connected to the SM200C.
- The Signal Hound SDK, for `device_apis/sm_series/lib/macos_arm/libsm_api.2.3.7.dylib`.
- Homebrew libusb. The library links it by absolute path and will not load
  without it:

```
brew install libusb
ls /opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib
```

- The Xcode command line tools, for `clang`.

## 2. Network setup

The device defaults to 192.168.2.10 on port 51665. It speaks UDP only, does not
answer ping, and has no TCP stack.

In System Settings, find the Thunderbolt Ethernet interface and set:

- Configure IPv4: Manually
- IP address: 192.168.2.2 (anything in the subnet except .10)
- Subnet mask: 255.255.255.0
- Router: leave blank
- MTU: Jumbo (9000)

The MTU is not optional. Data datagrams are 8200 bytes and will not fit inside a
1500 byte MTU.

macOS may label the interface "Self-assigned IP" even when the manual address is
applied correctly, because there is no router. Confirm the real state from the
terminal:

```
networksetup -listallhardwareports     # find the enX device
ifconfig en6                           # expect your address and mtu 9000
```

Then check the device is awake. Ping fails by design, but it populates the ARP
table:

```
ping -c1 -W1 192.168.2.10
arp -n 192.168.2.10
```

A real MAC address rather than `(incomplete)` means the link is good.

No sysctl tuning is needed. See "Socket buffer" below for why.

## 3. Build

```
make
```

That produces `sem_shim.dylib`, used by the Python backend, and
`native/libsmnative.dylib`, the native backend.

The macOS build programs the device's I/Q decimation filters with impulses
instead of low-pass filters, which makes I/Q streaming return zeros. Both
backends replace each impulse with the filter the Linux build would design for
the device's current rate mode, decimation and bandwidth. Nothing needs
extracting for that. To check the design on this machine:

```
python3 sm_filters.py selftest
```

The Linux builds also contain constant tables for these filters, which the
streaming engine never sends. For comparison, extract them once from either
Linux library and select them with `--filter-repair tables`:

```
python3 sm_filters.py extract path/to/lib/aarch64/libsm_api.so.2.3.9
```

That writes `filter_tables.json` in the current directory; it must sit beside
the Python scripts. It is read from your copy of the SDK rather than stored in
this repository because it is Signal Hound's data.

## 4. First run

Copy `libsm_api.2.3.7.dylib` next to the scripts, or point at it by path. The
leading `./` matters: a bare filename makes `dlopen` treat it as a library to
search for and it will trawl Homebrew and your Python install before failing.

```
python3 sm_transport.py ./libsm_api.2.3.7.dylib 192.168.2.2 192.168.2.10 51665
```

Expected output:

```
api 2.3.7  slide 0x...
filter repair on: designed from the device's settings (decimation at SmDevice+0xca88)
patched 10 vtable slots at 0x...
redirected 4 semaphore imports to .../sem_shim.dylib
  SO_RCVBUF 8388608 (asked 100000000)
  AllocateResources 192.168.2.2 -> 192.168.2.10:51665 ok (fd 3)

smOpenNetworkedDevice -> 0 (No error)  handle 0
```

Handle 0 and "No error" means the device opened and its calibration data came
off the flash through the replacement transport. Everything downstream depends
on this working, so do not move on until it does.

## 5. Capture I/Q

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --center 1e9 --decimation 256 --seconds 2
```

Useful options:

| option | meaning |
| --- | --- |
| `--center` | one frequency in Hz, or several separated by commas to retune between captures |
| `--decimation` | power of two; sample rate is 200 MS/s divided by this |
| `--seconds` | dwell per frequency |
| `--short` | 16 bit complex shorts instead of 32 bit complex floats |
| `--ref-level` | dBm, with the attenuator on automatic |
| `--queue-ms` | smaller queue means faster retunes and less tolerance to interruption |
| `--out` | output path; several frequencies each get a suffix |
| `--filter-repair` | `design` (default), `tables`, or `off` to send the library's own uploads |
| `--no-filter-repair` | same as `--filter-repair off` |
| `--backend` | `native` (default) or `python`; Python only keeps up to about 25 MS/s |
| `--discard` | read the stream but write nothing, to test the link without the disk |
| `--no-promote` | native only: leave the library's thread priorities alone |

The native backend is the default. With `--backend python` the receive loop is
Python, one datagram at a time, which holds about 25 MS/s; it is there for
comparison. The old `--native` flag is still accepted and does nothing.

Retuning is `smSetIQCenterFreq` followed by `smConfigure`, and `smConfigure`
calls `smAbort` internally, so every hop tears down the engine thread and its
semaphores and builds new ones. The frequency printed is read back with
`smGetIQCenterFreq` rather than echoed, so you see what the device actually did.

## 6. Full rate: decimation 1

Decimation 1 is 200 MS/s: 800 MB/s off the wire, about 97,600 datagrams a
second. The native backend, the default, points the ten transport methods at C
functions and receives on a dedicated thread, so no Python runs on the data path.

Before a run:

- Mains power, with Low Power Mode off.
- Capture to the internal SSD. Full rate in 16-bit samples is 800 MB/s
  sustained, 48 GB a minute, which an external USB drive will not keep up with.
- Use `--short`. The device produces 16-bit samples, so floats only double the
  memory and disk traffic.

Then, in this order:

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --decimation 1 --short --seconds 10 --discard
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --decimation 1 --short --seconds 10
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --decimation 1 --short --seconds 10 --discard --no-promote
```

The first proves the link and the transport with the disk out of the picture.
The second adds the disk. The third is the A/B for the thread priority fix
described in `docs/findings.md`: if it loses data where the first did not, the
fix matters.

Below decimation 8 the queue size defaults to its maximum, 16 requests or
41.9 ms. Each request is one 2 MB transfer at decimation 1, so that is 32 MB the
device is allowed to send ahead of the engine.

### Reading a full-rate run

Each capture ends with either `clean: no loss anywhere` or `DATA LOSS:` and a
list. Losses can come from three places, and they point at different things:

- **Datagrams lost in transit** were dropped between the adapter and the
  socket. Run `netstat -s -p udp | grep -i 'full socket'` before and after. If
  that counter moved, the receiver thread was held off for longer than the 8 MB
  socket buffer lasts. If it did not, the drop was in the adapter or its driver.
- **Blocks flagged by the device as sample loss** mean the device's own buffer
  overflowed because requests reached it too slowly. The library also prints
  `I/Q data loss N` when this happens. It points at the library's engine thread
  or, indirectly, at the reader.
- **Library backlog** is how far the reader fell behind the library's 500 ms
  buffer. A peak near 500 ms means the disk or the reading loop is too slow.

The session lines at the end:

- **socket buffer** should read 8 MB and the **receiver thread** `real-time`.
  If it reads `user-interactive QoS`, the kernel refused the real-time policy.
  Setting `SMN_NO_REALTIME=1` forces that fallback deliberately.
- **deepest queue** is the most transfers armed at once; expect 16.
- **queue ran dry** counts transfers that finished with nothing else armed,
  meaning the device briefly had no request outstanding. One per capture is the
  stop at the end. More means the engine thread fell behind.
- **library thread seen at ... priority** is what the engine's thread actually
  had. Fixed priority 10 confirms the demotion described in the findings.

If the transport loses datagrams but the device flags nothing, try
`--queue-ms 21` (8 requests). That limits how much the device can burst at once
after a stall, at the cost of less slack for the engine thread.

### Holes

A lost datagram is not skipped. The native backend fills its place with zeros,
so every later sample stays at its true position in time, and the capture keeps
its full length. At decimation 1 one datagram is 2048 samples. When anything was
lost, the capture tool scans the file for these runs and lists them in the
sidecar as `[first_sample, length]`. The scan needs numpy.

## 7. Run the diagnostics

`sm_diag.py` runs a battery of experiments unattended on both backends and
writes one `report.json` and one self contained `report.html` into
`sm_diag_out/`, with each backend's own results and raw transfer dumps in
`sm_diag_out/python/` and `sm_diag_out/native/`. It needs numpy
(`pip3 install numpy`) and, for the native half, `make -C native`.

```
python3 sm_diag.py ./libsm_api.2.3.7.dylib --list
python3 sm_diag.py ./libsm_api.2.3.7.dylib --all
python3 sm_diag.py ./libsm_api.2.3.7.dylib --all --backends native
python3 sm_diag.py ./libsm_api.2.3.7.dylib --only sweep,iq-dec8-short
python3 sm_diag.py ./libsm_api.2.3.7.dylib --interactive
```

Both backends patch the same places in the library, so each runs in a process
of its own, Python first, then native, opening the device in turn. The
experiments marked native only, such as `iq-dec1-short`, run on the native
backend and show as skipped on the Python one. `--backends python` or
`--backends native` runs just one.

Each experiment is isolated. A failure is recorded and the run continues.

Every I/Q experiment is checked, and passes only if every check does:

| check | passes when |
| --- | --- |
| data | every requested sample was read, with no error |
| samples | over 0.5% of bytes are non-zero; for `iq-ab-repair-off`, that they are zeros |
| filters | all four stages were repaired (not checked with repair off) |
| holes | no datagram-sized stretch anywhere in the capture is all zeros |
| transport | no datagrams lost and no transfer timed out |
| library flags | no sample-loss or sync errors from `smGetIQ` |
| throughput | the harness read at least 95% of the sample rate |

A timestamps line is shown too, for information only: it compares each
block's timestamp with the first plus samples over rate. Until it is known
whether the library derives timestamps from the device or the host clock, it
does not count towards the verdict.

Each I/Q experiment also gets three charts in the report. The zero map covers
the whole capture: green where data arrived, red where a datagram-sized
stretch came back as exact zeros, and ticks where `smGetIQ` flagged sample
loss or a sync error. The I/Q trace shows the first 512 samples. The
spectrogram covers the samples kept for the report (`--keep-samples`, 65,536
by default), so a stretch of zeros shows as a dark band. Sweeps get their
trace.

The library has one connection-lost status per device, and a single failed
transfer or command sets it to -6 for good, after which every I/Q call fails.
The report shows that status after each setup call, so the first call that
failed is named, and each experiment starts with it cleared and records what it
found on entry and left on exit.

The filter A/B is built in. The `iq-ab-repair-on` and `iq-ab-repair-off`
experiments run the same settings with the designed repair and then with
repair off, in one session, so no second run into a separate directory is
needed. `iq-ab-repair-tables` repeats them with the constant tables, if
`filter_tables.json` is present. The report and
the console print a plain verdict comparing the two: if the repaired capture
has real samples and the raw one is zeros, the impulse filters are the cause and
the repair is the fix. If both stream data but both are zeros, the filters are
not the only problem and the setup path needs comparing against the Linux build.

The whole run respects `--filter-repair` (or `--no-filter-repair`) everywhere
except experiments that set it explicitly, so the A/B runs still do their own
thing.

`iq-atten0-again` repeats the experiment where the first framing slip was seen,
to show whether it is reproducible, and `iq-soak` streams for five seconds to
count how often slips happen. The framing column reports, per experiment,
transfers that arrived out of frame, resyncs, stale datagrams discarded, and
reads flagged with a sync error.

`iq-dec1-short` is native only. The Python transport cannot hold 200 MS/s, so a
broad run skips it; name it with `--only iq-dec1-short` to force it and watch it
fail, or capture at decimation 1 with the native backend instead.

The sweep experiments are the ones to read first. A sweep drives the same RF
chain and ADC without going near the I/Q engine, so real spectrum there
alongside empty I/Q localises a fault precisely.

Two things the harness captures that the API will not show you. Every command
packet sent to the device is logged and grouped by opcode. And the aux block at
the tail of each transfer is decoded using the field offsets in
`docs/findings.md`, including the status word whose bits 8 to 15 are the sync
counter the API itself uses to detect framing faults.

## 8. Reading the numbers

`sm_transport.py` reports per run:

- **datagram gaps** and **lost**: derived from the header counter. Non zero means
  the kernel dropped packets before we read them.
- **counter resets**: a backwards jump, which is the stream restarting rather
  than loss.
- **FinishDataXfer calls**, **datagrams**, **payload**: how much actually arrived.
- **full scans**: every hundredth transfer is measured in its entirety rather
  than sampled, so this is trustworthy about whether payloads are empty.
- **requested transfer sizes**: 32768 is the calibration read at open, 262144 is
  a streaming transfer.

`sm_iq_capture.py` adds sample loss flags. These come from a status bit in the
aux block, so they are the device reporting that its own buffer overflowed
because requests reached it too slowly. Datagram gaps mean packets were dropped
on the way in. The native backend's figures are described in section 6.

## 9. Working with captures

Output is interleaved complex, raw, with no header. Beside it,
`capture.iq.json` records the sample rate, centre, bandwidth, correction factor,
the timestamp of the first sample, what the transport lost during that capture,
and the positions of any holes.

```python
import numpy as np

x = np.fromfile("capture.iq", dtype=np.complex64)       # 32 bit float default

s = np.fromfile("capture.iq", dtype=np.int16)           # with --short
x = (s[0::2] + 1j * s[1::2]).astype(np.complex64)
```

With `--short` the samples are full scale and need the factor from
`smGetIQCorrection`, printed at capture time, to become amplitude corrected.
Multiply by it, then power in dBm is `10 * log10(|x|**2)`. Validate that against
a signal of known level before trusting absolute numbers.

A quick look at what you caught:

```python
import numpy as np
x = np.fromfile("capture.iq", dtype=np.complex64)
rate = 781250.0
spec = 20 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(x[:65536] * np.hanning(65536)))) + 1e-12)
freqs = np.fft.fftshift(np.fft.fftfreq(65536, 1 / rate))
print("peak offset", freqs[spec.argmax()], "Hz")
```

To keep zero-filled holes out of an analysis, mask them from the sidecar:

```python
import json
meta = json.load(open("capture.iq.json"))
for first, length in meta["holes"] or []:
    x[first:first + length] = np.nan
```

Put a known signal in before drawing conclusions. Getting bytes out is not the
same as getting correct bytes out.

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `dlopen` walks through Homebrew and pyenv paths and fails | bare filename is treated as a library name, not a path | prefix with `./` or give an absolute path |
| `Device not found (-2)` right after a Python traceback | ctypes swallows exceptions in callbacks and hands C a zero byte count, which looks like a short calibration read | fix the traceback; read it rather than the API status |
| `AllocateResources failed` | usually no route to the device, or the address is not on the interface | check `ifconfig` and the ARP table |
| Open hangs, or a retune hangs on the second hop | `sem_destroy` arriving while another thread sits in `sem_wait` | known race in the shim; its mutexes and condvars are created once per slot and never destroyed, which takes the worst of it away but does not close it |
| `SO_RCVBUF 8388608 (asked 100000000)` | macOS caps the request | expected, see below |
| datagram gaps at high rates with the Python backend | the Python loop fell behind | use the native backend, the default |
| `DATA LOSS: ... lost in transit` with the native backend | receiver thread held off, or drops in the adapter | see section 6 for telling the two apart |
| `I/Q data loss N` printed by the library | the device's buffer overflowed; requests fell behind | check the library thread priority line and the queue ran dry count |
| `transfer timeouts` | the device stopped answering; the library's error state is now stuck | close and reopen the device |
| every I/Q experiment reads "no data: Device connection lost" | the connection-lost status was already -6 | the setup status table in the report names the call that set it |
| `smNetworkedSpeedTest` fails and later calls report connection lost | it streams 32 MB at line rate, beyond the Python transport | do not call it with the Python backend |
| "Data synchronization error" from `smGetIQ` | an aux block was not where it belonged: the transfer boundaries slipped | samples are still delivered; the framing column and the misframed and resynced counts say how much was affected |
| "N unrequested datagrams were discarded" | data was waiting that no transfer asked for | expected after a slip; it heals the stream |
| `make test` fails with "simulator did not start" on macOS | 127.0.0.2 is not configured on loopback | `sudo ifconfig lo0 alias 127.0.0.2 up` |
| Samples look plausible but signals appear at the wrong frequency | aliasing from the stubbed filter at decimation 16 or above | use decimation 8 or below, or filter yourself |
| SFP diagnostics all zero | the transceiver does not report DDM | harmless |
| Sweeps work, every I/Q mode returns exact zeros | impulse decimation filters not repaired | check the "filter repair" line at startup; "filter design unavailable" means the library's layout has changed |
| `expected one 79-tap table, found 0` | wrong file given to `sm_filters.py extract` | point it at a Linux `libsm_api.so`, not the macOS dylib |

### Socket buffer

The library asks for a 100 MB `SO_RCVBUF` and treats refusal as fatal. macOS
caps this at `kern.ipc.maxsockbuf`, 8 MB on current Apple Silicon Macs, and
rejects anything larger. The sysctl itself cannot be raised past that ("Result
too large"), because the ceiling is derived from the mbuf cluster pool. Raising
the pool needs the `ncl` boot argument, which on Apple Silicon means lowering
the Mac's startup security. I would not.

Both backends negotiate the request down instead. At 103 MB/s nothing has been
dropped at 8 MB. At decimation 1, once kernel overhead per datagram is counted,
8 MB is roughly 5 to 10 ms of traffic, which is why the native backend's
receiver runs under the real-time policy.

## 11. Validating the filter fix

Getting non-zero samples is the first test, not the last. With a signal
generator or a known strong emitter:

- A tone at a known offset from centre should appear at that offset.
- Its level, after the `smGetIQCorrection` factor, should match the source.
- A tone placed just outside the selected bandwidth should stay out rather than
  fold into the passband. This is the test that shows the decimation filters are
  real, since an impulse would pass it straight through.

Do the out-of-band test at decimation 8 or below. Above that, the separate
host-side filter stub aliases regardless of what the device does.

## 12. Using it from Python

`sm200c.py` wraps the whole SM API for tuning, sweeps and the four I/Q modes
in one class, with the transport and filter repairs already applied. Both
scripts are built on it.

```python
from sm200c import SM200C, SweepListStep, Segment

with SM200C("./libsm_api.2.3.7.dylib") as sm:      # host, device, port default
    print(sm.info, sm.diagnostics())

    # Front end
    sm.ref_level = -20             # dBm; puts the attenuator on auto
    sm.attenuator = 2              # or a fixed 10 dB; None for auto
    sm.preselector = True

    # Sweeps
    info = sm.configure_sweep(center=2.442e9, span=80e6, rbw=30e3)
    trace = sm.sweep()             # trace.freqs, trace.min, trace.max (dBm)
    for trace in sm.sweeps(count=100, depth=4):    # queued in the device
        ...

    # I/Q streaming
    iq = sm.configure_iq(center=1e9, decimation=8)  # iq.sample_rate, iq.correction
    block = sm.read_iq(1 << 20)    # block.samples is complex64
    for block in sm.stream_iq(count=200):
        ...

    # 16-bit at full rate, reusing one buffer
    sm.configure_iq(center=1e9, decimation=1, data_type="int16")
    buf = sm.iq_buffer(1 << 20)
    block = sm.read_iq(1 << 20, out=buf)            # block.complex() to convert

    # I/Q sweep list, segmented and full band
    sm.configure_iq_sweep_list([SweepListStep(1e9, 4096), SweepListStep(2e9, 4096)])
    hop = sm.iq_sweep_list()       # hop.samples[i] per step
    sm.configure_segmented(center=2e9, segments=[Segment(100_000)])
    segs = sm.capture_segmented()
    sm.configure_full_band(samples=32768)
    cap = sm.full_band(sm.full_band_index(2.4e9))
```

API errors raise `SmError`, carrying the call, status and message. Warning
statuses, such as a setting being clamped, are issued as `SmWarning`, except
from the read calls: each `IQBlock` and `Sweep` carries its own status, and a
sync error on a read is reported as `block.sync_error` because the samples
still arrived. `read_iq` purges the API's buffer on the first read after
`configure_iq`.

The native backend is the default and must be built (`make -C native`).
`backend="python"` exists for the diagnostics' side-by-side runs. A process
can load the library with one backend only, so the diagnostics run each
backend in a process of its own.

Only streaming and sweeps have been run on this transport. The sweep list,
segmented and full-band calls follow `sm_api.h` exactly but are untested here,
and the header describes segmented capture as an SM200B and SM435B feature,
so an SM200C may refuse it.

## 13. Extending it

**Adding a diagnostic experiment.** Add an entry to `EXPERIMENTS` in
`sm_diag.py`: a name, the kind (`iq` or `sweep`), a config dict, and a sentence
saying what it discriminates. The runner, the report and the HTML pick it up
without further change.

**Testing the native transport.** `make test` runs `native/test_native.py`,
which starts `sm_sim`, a stand-in for the device, and drives the transport the
way the library's I/Q engine does. Every simulated datagram carries its true
stream position, so the tests check that each one lands exactly where it
belongs: across counter wraps, loss, duplicates and counter restarts. On macOS
the simulator needs a second loopback address, once per boot:

```
sudo ifconfig lo0 alias 127.0.0.2 up
```

**Receiving faster.** The native backend makes one `recvmsg` call per datagram.
Darwin's `recvmsg_x` receives a batch per call, but it is private API. Try it
only if a full-rate run shows the receiver thread cannot keep up.

**Surviving a library update.** Offsets are resolved by symbol name through
`LC_SYMTAB` and the indirect symbol table, not hard coded, so a minor version
bump should still work. A change to the `LinuxSockInterface` virtual method
order will not, and shows up as a crash rather than a quiet wrong answer. If
that happens, re-derive the vtable layout as described in `docs/findings.md`.
