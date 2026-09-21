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

## 3. Build and extract the filter tables

```
make
```

That produces `sem_shim.dylib`, used by the Python backend, and
`native/libsmnative.dylib`, the native backend.

The macOS build programs the device's I/Q decimation filters with impulses
instead of low-pass filters, which makes I/Q streaming return zeros. The Linux
builds in the same SDK carry the correct coefficients. Extract them once from
either one:

```
python3 sm_filters.py extract path/to/lib/aarch64/libsm_api.so.2.3.9
python3 sm_filters.py selftest
```

That writes `filter_tables.json`. Both files must sit in the same directory as
the Python scripts, which is where they look for them. The tables are read from
your copy of the SDK rather than stored in this repository because they are
Signal Hound's data.

Without `filter_tables.json` everything still runs, and sweeps still work, but
I/Q streaming will return zeros.

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
filter repair on: shipped tables for [79, 159, 189] taps
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
| `--no-filter-repair` | send the library's own filter uploads, for comparison |
| `--native` | use the C backend; needed below decimation 8 |
| `--discard` | read the stream but write nothing, to test the link without the disk |
| `--no-promote` | native only: leave the library's thread priorities alone |

Without `--native` the receive loop is Python, one datagram at a time, which
holds about 25 MS/s. For anything faster see the next section.

Retuning is `smSetIQCenterFreq` followed by `smConfigure`, and `smConfigure`
calls `smAbort` internally, so every hop tears down the engine thread and its
semaphores and builds new ones. The frequency printed is read back with
`smGetIQCenterFreq` rather than echoed, so you see what the device actually did.

## 6. Full rate: decimation 1

Decimation 1 is 200 MS/s: 800 MB/s off the wire, about 97,600 datagrams a
second. Use `--native`, which points the ten transport methods at C functions
and receives on a dedicated thread, so no Python runs on the data path.

Before a run:

- Mains power, with Low Power Mode off.
- Capture to the internal SSD. Full rate in 16-bit samples is 800 MB/s
  sustained, 48 GB a minute, which an external USB drive will not keep up with.
- Use `--short`. The device produces 16-bit samples, so floats only double the
  memory and disk traffic.

Then, in this order:

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --native --decimation 1 --short --seconds 10 --discard
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --native --decimation 1 --short --seconds 10
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --native --decimation 1 --short --seconds 10 --discard --no-promote
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

`sm_diag.py` opens the device once and runs a battery of experiments unattended,
writing `report.json`, a self contained `report.html`, and a raw transfer dump
per experiment into `sm_diag_out/`.

```
python3 sm_diag.py ./libsm_api.2.3.7.dylib --list
python3 sm_diag.py ./libsm_api.2.3.7.dylib --all
python3 sm_diag.py ./libsm_api.2.3.7.dylib --only sweep,iq-dec8-short
python3 sm_diag.py ./libsm_api.2.3.7.dylib --interactive
```

Each experiment is isolated. A failure is recorded and the run continues.

Run it twice, once normally and once with `--no-filter-repair`, into separate
output directories. The difference between the two is the filter fix and
nothing else, which is the cleanest possible confirmation of it.

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
| datagram gaps at high rates with the Python backend | the Python loop fell behind | use `--native` |
| `DATA LOSS: ... lost in transit` with `--native` | receiver thread held off, or drops in the adapter | see section 6 for telling the two apart |
| `I/Q data loss N` printed by the library | the device's buffer overflowed; requests fell behind | check the library thread priority line and the queue ran dry count |
| `transfer timeouts` | the device stopped answering; the library's error state is now stuck | close and reopen the device |
| `make test` fails with "simulator did not start" on macOS | 127.0.0.2 is not configured on loopback | `sudo ifconfig lo0 alias 127.0.0.2 up` |
| Samples look plausible but signals appear at the wrong frequency | aliasing from the stubbed filter at decimation 16 or above | use decimation 8 or below, or filter yourself |
| SFP diagnostics all zero | the transceiver does not report DDM | harmless |
| Sweeps work, every I/Q mode returns exact zeros | impulse decimation filters | extract `filter_tables.json` as in step 3 |
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

## 12. Extending it

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
