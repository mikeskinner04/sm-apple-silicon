# Binary analysis notes

Everything here was recovered statically from `libsm_api.2.3.7.dylib`, the macOS
arm64 build shipped in `device_apis/sm_series/lib/macos_arm/`. Addresses are file
addresses in that build. Resolve them by symbol at runtime rather than hard
coding, since they move between versions.

## The library

Mach-O arm64, install name `libsm_api.2.dylib`, current version 2.3.7. Minimum
macOS 12, built against the 15.2 SDK, code signed. It links
`/opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib` by absolute path, plus
`libc++` and `libSystem`.

Calibration data is cached under `~/.signal_hound/` as `sm%08d.bin`, and errors
are logged to `sm200_error_log.txt` in the same place.

## What is missing

No socket receive function is imported. The complete set of relevant imports is
`socket`, `bind`, `connect`, `send`, `setsockopt`, `getsockopt`, `inet_addr`,
`close`. There is no `recv`, `recvfrom`, `recvmsg`, `recvmmsg`, `read`, `poll`,
`select`, `kqueue`, `kevent`, `dlopen` or `dlsym`, so there is no way the receive
happens indirectly either.

Two leftover format strings confirm the intent: `recvmmsg recieved %d of %d
msgs` and `recvmmsg invalid number of bytes returned, %d of %d`, both referenced
from `LinuxSockInterface::FinishDataXfer`.

Supporting evidence that the file was compiled from Linux sources without Darwin
adaptation: the `msghdr` structures it builds have a 56 byte stride with a
`size_t msg_iovlen`, which is the Linux layout. Darwin's `msghdr` is 48 bytes
with an `int` at that offset.

## Transport vtable

`__ZTV18LinuxSockInterface` at 0x68500. Objects hold a vptr of vtable + 0x10.
All ten transport methods are contiguous:

| vptr offset | method |
| --- | --- |
| +0x10 | `AllocateResources(const char *host, const char *dev, uint16_t port)` |
| +0x18 | `Deallocate()` |
| +0x20 | `BeginCommandXfer(int, const uint8_t *)` |
| +0x28 | `FinishCommandXfer(int)` |
| +0x30 | `BeginDataXfer(int, int, int)` |
| +0x38 | `BeginDataXfer(int, uint8_t *, int, int)` |
| +0x40 | `FinishDataXfer(int)` |
| +0x48 | `Data(int) const` |
| +0x50 | `TimedOut(int) const` |
| +0x58 | `XferLenRequested(int) const` |

`DeviceInterfaceNetworked` holds the interface pointer at offset 0x40 and tail
calls straight through it. Its `BeginCommandXfer` is four instructions: load the
pointer, load the vptr, load the slot, branch. Everything above the transport
goes through these ten slots and nothing else.

## Transport contract

`AllocateResources` does, in order:

1. `socket(AF_INET, SOCK_DGRAM, 0)`, stored at `this + 0x10`
2. `setsockopt(SOL_SOCKET, SO_RCVTIMEO, {2s, 0}, 16)`
3. `setsockopt(SOL_SOCKET, SO_REUSEADDR, 1, 4)`
4. `setsockopt(SOL_SOCKET, SO_RCVBUF, 100000000, 4)`, fatal on failure
5. `getsockopt(SO_RCVBUF)` readback, warns if clamped
6. `bind(hostIP:port)` then `connect(deviceIP:port)`, same port both ends
7. allocates 32 slot buffers of 256 * 8192 = 2 MB each

`BeginCommandXfer` sends exactly 0x800 bytes. `FinishCommandXfer` returns the
byte count and resets it. `BeginDataXfer(idx, len, arg3)` records `len` and
computes `len / 8192` as the datagram count; nothing in the disassembly reads
`arg3` back. `FinishDataXfer(idx)` returns the byte count received.

`DeviceInterfaceNetworked::FinishDataXfer` returns a struct by sret:

| offset | field |
| --- | --- |
| +0x00 | `TimedOut(idx)` |
| +0x04 | `XferLenRequested(idx)` |
| +0x08 | bytes received |
| +0x10 | `Data(idx)` |

and sets the interface status at `DeviceInterface + 0x38` to -6 if the transfer
timed out or if received does not equal requested. Nothing on the networked path
ever sets it back to 0; the only store of zero to that field is in
`DeviceInterfaceUSB::AlignXfers`. See "Streaming I/Q" below for what that does
to the engine.

## Wire format

Globals at 0x6c000: 256 (max datagrams per transfer), 8200 (datagram size on the
wire), 8192 (payload size), 1.

Each data datagram is 8200 bytes: an 8 byte header followed by 8192 bytes of
payload. The original scatters these with two iovecs per message, the header
into a side array and the payload contiguously into the slot buffer. This is why
the interface MTU must be 9000.

The header's first four bytes carry a little-endian datagram counter that
increments by one per datagram and runs on across transfers, streams and host
sessions. It is 16 bits wide in practice: in one hardware run it went from
0xFFFF to 0 with the upper half unchanged, which a 32-bit counter would not do.
At decimation 1 it wraps about 1.5 times a second, so sequence arithmetic has to
be modulo 65536. That arithmetic is also correct if the counter turns out to be
wider. The unused string `UDP Counters: %d %d, Delta: %d` is what the counter
was for.

The last 8192 bytes of each transfer are the aux block, not I/Q. The engine
reads it from `dataPtr + xferLenRequested - 8192`.

## Command format

Commands are 32-bit little-endian words inside the fixed 2048 byte packet, with
the opcode in the top byte. Observed builders:

| opcode | meaning |
| --- | --- |
| 0x13 | initiate streaming I/Q; also stop, as `0x13000100` |
| 0x23 | fetch streaming I/Q |
| 0x04 | set UDP fragmentation, two words, `0x04010001` then the flag |

`CommandList::AddInitiateStreamingIQ(uint8_t a, uint16_t b, bool c)` builds one
word: bytes 0 and 1 are `b`, byte 2 is `a` plus 0x10 when `c`, byte 3 is 0x13.

## Aux block

Offsets used by `AuxStatusReader::Update`:

| offset | meaning |
| --- | --- |
| +0x1c | status dword |
| +0x22 | uint16 |
| +0x24 | uint32 |
| +0x2c | uint32 |
| +0x40, +0x42, +0x44, +0x46 | uint16 values scaled into temperatures and voltages |

Within the status dword: bit 0, bits 1 to 4, bit 5 and bit 6 each set a separate
flag, and bits 8 to 15 are a counter compared against the previous value. A
mismatch is what the API reports as a framing or sync fault.

## Stubbed DSP primitives

Most of `DSP` is implemented, including `Convert_16sc32fc_Sfs`, `SampleDown_32fc`,
the FFT forward path and the window generators. These are not:

| function | size | behaviour |
| --- | --- | --- |
| `FIR_32fc::SetTaps` | 4 bytes | bare `ret`, taps discarded |
| `FIR_32fc::Filter` | 32 bytes | `memcpy(dst, src, len * 8)`, returns 1 |
| `FIR_32f::SetTaps` | 4 bytes | bare `ret` |
| `FIR_32f::Filter` | 8 bytes | returns 1, writes nothing |
| `IIR_32f::SetTaps`, `Filter`, `Filter_I` | 4 bytes | bare `ret` |
| `FFT_32fc::InvFFT` | 4 bytes | bare `ret` |
| `GetLowpassIIRTaps_32f`, `GetHighpassIIRTaps_32f` | 4 bytes | bare `ret` |
| `Transpose_32f_C1R` | 4 bytes | bare `ret` |
| `InitIpps` | 4 bytes | bare `ret`, expected on ARM |
| `AVX2Supported` | 8 bytes | returns false, correct on ARM |

`EngineIQStreamingNetworked::XferThreadSoftwareDecimation` calls, in order:
`GetLowpassFIRTaps_32fc`, `FIR_32fc::SetTaps`, `Convert_16sc32fc_Sfs`,
`FIR_32fc::Filter`, `SampleDown_32fc`, `FIR_32fc::Filter`, `SampleDown_32fc`,
`StoreIQ`. With the taps discarded and the filter reduced to a copy, that chain
downsamples without filtering. The consequence is aliasing at any decimation of
16 or above, where software decimation is engaged. Decimation 8 and below is
hardware only and runs `XferThreadDirect` instead.

The comparison with the Linux builds below confirms these particular stubs are
shared with Linux aarch64 and absent from the IPP backed x86 build.

## Comparison with the Linux builds

The same SDK ships an aarch64 Linux build (2.3.9) and an x86-64 Linux build
(2.3.10). Setting the three side by side separates platform gaps from
architecture gaps.

| | macOS arm64 2.3.7 | Linux aarch64 2.3.9 | Linux x86-64 2.3.10 |
| --- | --- | --- | --- |
| imports `recvmmsg` | no | yes | yes |
| Intel IPP linked | no | no | yes, 135 symbols |
| `GetLowpassFIRTaps_64f` | 76 B, impulse | 396 B, real | 450 B, real |
| `FIR_32fc::SetTaps` | 4 B, stub | 4 B, stub | 589 B, real |
| `FIR_32fc::Filter` | 32 B, `memcpy` | 32 B, `memcpy` | 86 B, real |
| `FFT_32fc::InvFFT` | 4 B, stub | 4 B, stub | 69 B, real |
| `GetLowpassIIRTaps_32f` | 4 B, stub | 4 B, stub | 632 B, real |

So the missing receive path and the impulse taps generator are macOS gaps. The
host-side FIR stubs are ARM gaps, shared with Linux aarch64, and only the IPP
backed x86 build filters correctly on the host.

## The I/Q zeros

`SmDevice::ConfigureIQStreamingNet` programs four decimation filter stages in
the device. In the macOS build each stage is allocated, zeroed, filled by
`GetLowpassFIRTaps_64f`, then sent with
`CommandList::WriteIQFilter(int, const std::vector<double> &)`.

On macOS `GetLowpassFIRTaps_64f` is the whole of this:

```
if (!taps) return;
bzero(taps, n * 8);
taps[n / 2] = 1.0;
```

It ignores the cutoff, the window and the normalise flag. The result is a unit
impulse.

In the aarch64 build the same function is a real design, `sin(x) / x` with
`x = k * 2 * pi * fc` for `k` from `-n/2` to `n/2`, 1.0 at the centre, an
optional exact Blackman window, then normalisation by the sum. `n` must be odd
and only window types 1 (Blackman) and 4 (none) are accepted. The window is

```
w[i] = 0.42659 - 0.49656 cos(2 pi i / (N - 1)) + 0.076849 cos(4 pi i / (N - 1))
```

The aarch64 and x86 builds use it for the four fixed stages. They also call
`WriteIQFilter(int, const double *, int)` on constant tables, bit for bit
identical in both builds, which earlier versions of this repair substituted:

| stage | taps | centre / sum | approximate cutoff |
| --- | --- | --- | --- |
| 1 | 189 | 0.178 | 0.089 |
| 2 | 79 | 0.443 | 0.221 |
| 3 and 4 | 159 | 0.424 | 0.212 |

But `ConfigureIQStreamingNet` writes the tables into a separate command list,
the third one passed to the `EngineIQStreamingNetworked` constructor. The
engine stores it (+0x650 on Linux, +0x648 on macOS) and only its destructor
touches it again; `XferThreadDirect` and `XferThreadSoftwareDecimation` send
the first list, which holds the run-time designs. So the device on Linux gets
designed filters, and the tables are dead code. That is also why the design
only matched the tables to within a few thousandths: they are different
filters.

The cutoffs, the same in macOS 2.3.7 and Linux 2.3.9, in cycles per sample at
each stage's input:

- The bandwidth is first clamped to between 1% and 82.5% of the output rate
  and written back to `SmDevice`.
- The defaults are 0.08, 0.2, 0.2 and 0.2.
- At decimation 1, 2, 4 or 8 the last stage in use is set to
  `bandwidth * 1.02 / 2` over its input rate, then clamped to at least 0.02
  and at most 0.083325, 0.2, 0.2 or 0.225. The input rates are a fixed 1 GHz
  for stage 1, then the base rate, half it and a quarter of it.
- At decimation 16 and above all four keep the defaults.
- Decimation 4 and 8 skip the narrowing if the word beside the decimation is
  set, but `SetIQBandwidth` always writes 0 there.

`SmDevice` holds the rate mode at +0xca78, the decimation at +0xca88 and the
bandwidth at +0xca90 in the macOS build. The repair decodes these from the
setters rather than hard coding them. `sm_filters.py` reproduces the cutoffs,
the design and the encoding; `reference/ref_filters.c` calls the Linux
library's own functions, and `reference/check_design.py` compares the two bit
for bit.

## WriteIQFilter encoding

Per stage:

| stage | taps | scale | address |
| --- | --- | --- | --- |
| 1 | 189 | 2^19 | 0x478 |
| 2 | 79 | 2^18 | 0x578 |
| 3 | 159 | 2^18 | 0x678 |
| 4 | 159 | 2^18 | 0x778 |

Taps are summed over all `n`, divided by the sum, multiplied by the scale and
truncated to int32. Because the taps are symmetric only `(n + 1) / 2` values
are sent, edge to centre inclusive. The command is

```
word 0      (0x04 << 24) | (payload_words << 16) | address
words 1-8   zero
words 9..   (n + 1) / 2 int32 coefficients
```

with `payload_words = (n + 1) / 2 + 8`. So the stage 2 header is `0x04300578`.

An impulse sums to 1.0, so normalisation leaves it alone and the centre becomes
exactly the scale value, the last word of the command after a run of zeros:

| stage | impulse centre | shipped centre |
| --- | --- | --- |
| 1 | 524288, 20 bits | 93545, 17 bits |
| 2 | 262144, 19 bits | 116254, 17 bits |
| 3 and 4 | 262144, 19 bits | 111021, 17 bits |

If the device's coefficient field is narrower than the impulse needs, the value
truncates and the decimator outputs zeros. That last step is inferred rather
than observed, since the FPGA is not visible from the library. It matches every
measurement: correct rates, an unbroken sequence counter, a live aux block, and
a sample region of exact zeros rather than noise.

Sweep modes upload their filters with `CommandList::AddFIRCoeffs` instead.
`WriteIQFilter` is only called from the networked I/Q configure path, which is
why sweeps work and I/Q does not.

## Command format, revised

Opcode `0x04` is a prefix rather than a single command. The low 16 bits select
a register or subcommand and the next byte up is a word count, for example
`0x04010001` for UDP fragmentation, `0x04010022` and `0x04010023` for the high
and low halves of the 64-bit parameter to `AddInitiateStreamingIQHiRes`, and the
`WriteIQFilter` headers above.

## Signal Hound's stated limitations

The macOS readme shipped with the SDK says, for the ARM build: networked
C-models are not supported, only sweep and I/Q streaming modes are available,
I/Q decimation is limited to 1, 2 and 4, and the software filter cannot be
disabled, so aliased and spurious signals can appear in the rejection regions
while the passband is unaffected. It recommends an additional FIR filter after
acquisition.

Read against the binary:

The aliasing statement describes the `GetLowpassFIRTaps_64f` placeholder exactly.
A unit impulse passes everything unchanged, passband included. On the USB path
those taps feed the host-side `FIR_32fc`, itself a `memcpy`, so the result is the
documented aliasing and nothing worse. On the networked path the same impulse is
uploaded into the device's hardware decimator by `WriteIQFilter`, a path the
vendor never ran because C-models are unsupported. That is why aliasing is
documented and zeros are not.

So the placeholder is known and deliberate rather than an oversight. The filter
repair here passes real coefficients through untouched, so a future build that
replaces it needs no change.

The decimation limit of 1, 2 and 4 keeps USB I/Q on hardware-only decimation,
away from the stubbed software chain. On the networked path hardware-only
decimation extends to 8, so staying at 8 or below is consistent with the
vendor's own guidance.

## Semaphores

`sem_init`, `sem_wait`, `sem_post` and `sem_destroy` are ordinary libSystem
imports reached through stubs, so they are interposable through the GOT. Their
slots can be found by name through the indirect symbol table rather than by
disassembling the stubs.

`sem_init` is called with its return value discarded, for example
`sem_init(sem, 0, 0)` followed immediately by an unrelated store. Every engine
class except `EngineSweepNarrow` uses these: audio, custom, sweep fast, real time
hardware and software, I/Q sweep list, segmented I/Q, I/Q streaming USB and I/Q
streaming networked. Nothing beyond device open and plain command and response
works on the networked path without the shim.

`EngineIQStreamingUSB` and `EngineSweepFast` are both modes Signal Hound's macOS
readme lists as supported, so the gap reaches beyond the unsupported networked
path. On Darwin a `sem_wait` on a semaphore that was never created returns an
error immediately instead of blocking, so these engines most likely spin rather
than wait. That can look like working on a fast machine. Whether it also causes
races depends on surrounding synchronisation that has not been traced.

`sem_t` is an `int` on Darwin, four bytes, too small to hold a pointer, so the
shim stores a table index in it. It uses a pthread mutex and condvar rather than
`dispatch_semaphore` because dispatch deliberately aborts the process when a
semaphore is disposed with its count below the initial value, and the library's
teardown order is not under our control.

## Streaming I/Q

`EngineIQStreamingNetworked` runs one of two transfer threads, both named
`SM I/Q Networked Thread`. Up to decimation 8 it is `XferThreadDirect`, where
all decimation happens in the device. Above 8 it is
`XferThreadSoftwareDecimation`: the device decimates by 8 and the host does the
rest, through the stubbed FIR described above.

### Sizing, from the constructor

| field | value |
| --- | --- |
| `+0x4d0` requests in flight | `round(queue_ms / 2.62144)`, clamped to 2..16 |
| `+0x4d4` transfer bytes | `0x200000 / PatchSetup[+0x204]` |
| `+0x4d8` samples stored per transfer | `(transfer bytes - 8192) / 4` |

`PatchSetup[+0x204]` is the hardware decimation: at decimation 8 the transfer
was 262,144 bytes on hardware. So a transfer is 2 MB at decimation 1 and always
lasts 2.62 ms, which is where the 2.62 ms in the documentation of
`smSetIQQueueSize` comes from. At decimation 1 the maximum queue is 16 transfers,
32 MB, 41.9 ms.

The library's own circular buffer between the engine and `smGetIQ` holds half a
second of samples.

### The loop

`XferThreadDirect` primes every slot with `BeginDataXfer(i, bytes, 2000)`
followed by a request command, then loops over the slots in order:

1. `FinishDataXfer(i)` and `FinishCommandXfer(i)`.
2. If `TransferStatus()` is non-zero, `SystemSleep(32)`.
3. `UpdateAuxData` on the last 8192 bytes, then `StoreIQ` on the rest, always
   with the fixed sample count from `+0x4d8`, whatever was received.
4. Re-arm slot `i` and send its request.

On stop it sends stop-loop, stop-streaming and disable-GPIO commands on the
spare slot and drains every outstanding transfer without storing it.

So streaming is request driven. The device sends one transfer per request and
nothing it was not asked for, and at most the queue's worth is ever in flight.
The documentation's "once data is requested, its transfer must be completed"
matches.

Step 2 matters most. The status it checks is the sticky -6 from the transfer
contract, so after one short or timed-out transfer, every later transfer is
followed by a 32 ms sleep for the life of the device handle. At decimation 1 a
transfer lasts 2.62 ms, so the device would be starved from then on. Because
step 3 stores the whole buffer regardless, reporting a short transfer gains
nothing. The native backend therefore zero-fills lost datagrams in place and
reports stream transfers (32 datagrams or more) complete, keeping its own
account of the loss. A stream transfer that received nothing before the timeout
is still reported as timed out, and transfers under 32 datagrams, the
calibration and flash reads at open, keep the original strict behaviour.

### Loss reporting

`StoreIQ` reads flags that `UpdateAuxData` copies from the aux block into
`SmDevice`. The byte at `+0x39` sets the sample loss flag `smGetIQ` returns, and
the bytes at `+0x3b` and `+0x42` make it print `I/Q data loss %d`. Both are the
device reporting that its own buffer overflowed, meaning requests reached it too
slowly. Neither reflects datagrams lost on the way in.

### Thread priority

The constructor calls `SetThreadPriorityTimeCritical` on the transfer thread.
That function is `pthread_setschedparam(thread, 4, {10})`, which is `SCHED_FIFO`
at priority 10 in Darwin's numbering. On Linux that is a modest real-time
priority. On Darwin, libpthread passes it to `thread_policy` as `POLICY_FIFO`
with base priority 10. Reading XNU's
`thread_set_mode_and_absolute_pri`, a priority below 64 is taken relative to the
default of 31 and applied in fixed mode, which would leave the thread at fixed
priority 10: below every default thread, and in the band the scheduler steers
towards the efficiency cores. This is from the source, not observed. The native
backend reads the calling thread's policy and priority with `thread_info` every
64th `FinishDataXfer`, records the lowest it sees, and moves any thread below
timeshare 47 to timeshare 47 with `pthread_setschedparam(SCHED_OTHER, 47)`.
`sm_iq_capture.py --no-promote` turns the correction off for comparison.

## The connection-lost status

`DeviceInterface + 0x38` is set to -6, `smConnectionLostErr`, by any failed
command (`SendCommands`), data transfer (`FinishDataXfer`), aux fetch
(`GetAuxData`) or flash operation. The only code that sets it back to 0 is
`DeviceInterfaceUSB::AlignXfers`, called once from the USB `Connect`. So by
design -6 is terminal for a device handle: after one failure the handle is
finished until it is closed and reopened.

How it shows up:

- `SmDevice::Configure` calls `TransferStatus()` and ignores the result, so
  `smConfigure` still reports success.
- `EngineIQStreamingNetworked::GetIQ` checks it first, so `smGetIQ` returns
  "Device connection lost" at once, before the engine has requested any data.
- `EngineSweepNarrow::GetSweep` fills the sweep and then returns -6 with it.

`GetAuxData` builds `AddUpdateAuxFromFX3Hack` and `AddFetchAux(4)`, arms slot 0
for 32 KB, sends the commands and reads the reply. It is behind
`smGetDeviceDiagnostics`, `smGetSFPDiagnostics`, `smGetGPSState`,
`smGetFullDeviceDiagnostics` and `SmDevice::GetRFBoardTemperature`, which
`Configure` calls every time. `SmDevice::UpdateAuxData` ignores its result and
parses the buffer anyway, so after a failed fetch it decodes an uninitialised
8 KB buffer as device status.

`smNetworkedSpeedTest` arms 16 transfers of 2 MB, requests each with
`AddFetchUDPDummyData(256)`, and re-arms as they complete for the length of the
test: 32 MB of dummy data in flight at line rate. Any short transfer fails the
test and sets the status. The Python transport cannot drain that, so the speed
test always failed there and left the status at -6.

The diagnostics harness ran it as its last setup step, immediately before the
first experiment. The run on 2026-09-22 shows the result: the sweeps returned
data with a -6 status, and every I/Q experiment failed in `smGetIQ` having
captured nothing, which the report labelled as zeros. An earlier run reported
the same pattern and almost certainly had the same cause. The evidence that I/Q returns zeros comes only from
`sm_iq_capture.py` runs, which never called the speed test and did stream.

The same run confirmed the upload side of the zeros explanation on hardware:
all four `WriteIQFilter` uploads left the library as impulses and were
recognised and replaced by the repair.

`sm_transport.InterfaceStatus` reads and clears the field. It finds the device
table by the symbol `_deviceList` and decodes both offsets from the
instructions that use them, `SmDevice + 0x1048` from `UpdateAuxData` and
`+ 0x38` from `TransferStatus`, so a layout change fails loudly.

## Framing and the sync error

Every stream transfer ends with an aux block whose first four bytes are the
magic `0x31555834`, "4XU1". `SmDevice::UpdateAuxData(AuxData*, bool, bool)`
checks it on every transfer. With the magic present it clears a flag at
`SmDevice + 0xcb79` and parses the block; without it, it sets the flag and
skips the block. `SmDevice::HasSyncIssues` returns that byte, and
`EngineIQStreamingNetworked::GetIQ` checks it last, after copying the samples
out, returning -11, `smSyncErr`, "Data synchronization error". So a -11 from
`smGetIQ` still delivers the samples. The flag is not sticky: the next good aux
block clears it.

It means the datagram in the aux position was not an aux block: the host's
idea of where a transfer ends has slipped relative to the device's. A transport
that reads datagrams in arrival order never recovers from a slip. Surplus
datagrams, ones nobody consumed, sit at the front of every later transfer,
across reconfigures, because the aux fetch in `Configure` reads them too.

The run on 2026-09-25 showed exactly that. Everything through `iq-atten0` was
clean. Part way through `iq-atten0` the stream slipped, and every experiment
after it moved one transfer and then got -11. The interface status stayed 0
throughout, so no transfer came up short: loss would have left the final
transfer of the stream waiting for datagrams that never came, and set -6.
Something added datagrams. What, is not yet known; the Python transport now
records the sequence numbers around the first misframed transfer so the next
occurrence says.

Both transports now defend against it in two ways:

- **Flush at idle.** The library always arms a transfer before it sends the
  request for it. So when nothing is armed, anything waiting in the socket was
  never asked for, and it is discarded before the next transfer arms. The
  sequence continuity is carried through what is discarded, so the next
  transfer does not see a false gap. This heals a slip at the next configure
  at the latest.
- **Resynchronise on an early aux block.** An aux block arriving anywhere but
  the last slot of a stream transfer marks where the device's transfer really
  ended. What came before it in the current transfer belongs to the previous
  one, whose aux slot was already handed over, so it is discarded and the
  transfer fills afresh. The count of datagrams handed to the library then
  matches the device again, so later sample positions stay right. The damage is
  limited to the transfer where the slip happened: about one transfer of
  samples is suspect per event, which the counters report.

Without the resync, the simulator reproduces the hardware symptom exactly: a
single surplus datagram leaves every later transfer out of frame. With it, one
transfer is out of frame and the stream is exact from the next.

The magic check could in principle match an I/Q datagram whose first sample
happens to be I = 22580, Q = 12629. At decimation 1 that is roughly once in
twelve hours of full-scale noise, and far rarer at ordinary signal levels.

## Filter repair confirmed

On 2026-09-25 the built-in A/B ran both settings in one session: repair on gave
63.2% non-zero bytes, repair off gave exact zeros, everything else identical.
Real samples also came back at decimation 256, 16 and 8, in both sample
formats. 16-bit samples of ordinary noise have mostly zero high bytes, which is
why their non-zero byte count sits near 63% where 32-bit floats sit near 84%.
