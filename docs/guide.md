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

## 3. Build the shim

```
make
```

That produces `sem_shim.dylib`. It must sit in the same directory as the Python
scripts, which is where they look for it.

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

Start at a high decimation and work down. The receive loop is Python, one
datagram at a time, so it is comfortable to about 25 MS/s and will not hold
native rate.

Retuning is `smSetIQCenterFreq` followed by `smConfigure`, and `smConfigure`
calls `smAbort` internally, so every hop tears down the engine thread and its
semaphores and builds new ones. The frequency printed is read back with
`smGetIQCenterFreq` rather than echoed, so you see what the device actually did.

## 6. Run the diagnostics

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

The sweep experiments are the ones to read first. A sweep drives the same RF
chain and ADC without going near the I/Q engine, so real spectrum there
alongside empty I/Q localises a fault precisely.

Two things the harness captures that the API will not show you. Every command
packet sent to the device is logged and grouped by opcode. And the aux block at
the tail of each transfer is decoded using the field offsets in
`docs/findings.md`, including the status word whose bits 8 to 15 are the sync
counter the API itself uses to detect framing faults.

## 7. Reading the numbers

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

`sm_iq_capture.py` adds sample loss flags, which come from the API noticing its
own circular buffer overflowed. These fail differently: sample loss means our
receive loop fell behind, datagram gaps mean the kernel dropped them first.

## 8. Working with captures

Output is interleaved complex, raw, with no header.

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

Put a known signal in before drawing conclusions. Getting bytes out is not the
same as getting correct bytes out.

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `dlopen` walks through Homebrew and pyenv paths and fails | bare filename is treated as a library name, not a path | prefix with `./` or give an absolute path |
| `Device not found (-2)` right after a Python traceback | ctypes swallows exceptions in callbacks and hands C a zero byte count, which looks like a short calibration read | fix the traceback; read it rather than the API status |
| `AllocateResources failed` | usually no route to the device, or the address is not on the interface | check `ifconfig` and the ARP table |
| Open hangs, or a retune hangs on the second hop | `sem_destroy` arriving while another thread sits in `sem_wait` | known race in the shim; its mutexes and condvars are created once per slot and never destroyed, which takes the worst of it away but does not close it |
| `SO_RCVBUF 8388608 (asked 100000000)` | macOS caps the request | expected, no action, see below |
| datagram gaps at high rates | kernel buffer exhausted or the Python loop fell behind | raise the decimation, or move the hot path to C |
| Samples look plausible but signals appear at the wrong frequency | aliasing from the stubbed filter at decimation 16 or above | use decimation 8 or below, or filter yourself |
| SFP diagnostics all zero | the transceiver does not report DDM | harmless |

### Socket buffer

The library asks for a 100 MB `SO_RCVBUF` and treats refusal as fatal. macOS
caps this at `kern.ipc.maxsockbuf`, which is commonly pinned at 8 MB and rejects
larger values with `ERANGE` rather than clamping them. Raising it needs an `ncl`
boot argument, not a sysctl.

This transport negotiates the request downwards instead, so no tuning is
required. At 8 MB it has sustained 103 MB/s with an unbroken sequence counter
and no dropped datagrams.

## 10. Extending it

**Adding a diagnostic experiment.** Add an entry to `EXPERIMENTS` in
`sm_diag.py`: a name, the kind (`iq` or `sweep`), a config dict, and a sentence
saying what it discriminates. The runner, the report and the HTML pick it up
without further change.

**Moving the hot path to C.** `FinishDataXfer` in `sm_transport.py` is the only
performance critical function. A C version using Darwin's `recvmsg_x` to batch
receives would take native rate comfortably: 200 MS/s is about 97,600 datagrams
a second, which batched sixty at a time is a couple of thousand syscalls. Keep
the same ten method contract and the rest of the stack is unaffected.

**Surviving a library update.** Offsets are resolved by symbol name through
`LC_SYMTAB` and the indirect symbol table, not hard coded, so a minor version
bump should still work. A change to the `LinuxSockInterface` virtual method
order will not, and shows up as a crash rather than a quiet wrong answer. If
that happens, re-derive the vtable layout as described in `docs/findings.md`.
