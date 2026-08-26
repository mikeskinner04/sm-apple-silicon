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

and sets the device status to -6 if the transfer timed out or if received does
not equal requested.

## Wire format

Globals at 0x6c000: 256 (max datagrams per transfer), 8200 (datagram size on the
wire), 8192 (payload size), 1.

Each data datagram is 8200 bytes: an 8 byte header followed by 8192 bytes of
payload. The original scatters these with two iovecs per message, the header
into a side array and the payload contiguously into the slot buffer. This is why
the interface MTU must be 9000.

The header's first four bytes are a little-endian free running datagram counter
that increments by one per datagram. A backwards jump indicates the stream
restarting rather than loss. The unused string `UDP Counters: %d %d, Delta: %d`
is what this was for.

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

These look like gaps in the ARM port rather than macOS specifically, since the
x86 builds back these functions with Intel IPP. Worth checking the `lib/aarch64`
Linux build for the same holes.

## Semaphores

`sem_init`, `sem_wait`, `sem_post` and `sem_destroy` are ordinary libSystem
imports reached through stubs, so they are interposable through the GOT. Their
slots can be found by name through the indirect symbol table rather than by
disassembling the stubs.

`sem_init` is called with its return value discarded, for example
`sem_init(sem, 0, 0)` followed immediately by an unrelated store. Every engine
class uses these: audio, custom, sweep fast, real time hardware and software,
I/Q sweep list, and I/Q streaming networked. Nothing beyond device open and plain
command and response works without the shim.

`sem_t` is an `int` on Darwin, four bytes, too small to hold a pointer, so the
shim stores a table index in it. It uses a pthread mutex and condvar rather than
`dispatch_semaphore` because dispatch deliberately aborts the process when a
semaphore is disposed with its count below the initial value, and the library's
teardown order is not under our control.
