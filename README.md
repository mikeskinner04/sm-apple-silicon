# SM200C on macOS

Signal Hound's SM200C is a 10GbE spectrum analyser. The vendor does not support
it on macOS. This repository makes it work.

The macOS arm64 build of `libsm_api` ships in the SDK under
`device_apis/sm_series/lib/macos_arm/` and exports the full public API,
`smOpenNetworkedDevice` included, so networked support looks present. It is not.
The library imports no socket receive function at all: no `recv`, `recvfrom`,
`recvmsg`, `recvmmsg`, `read`, `poll`, `select`, `kqueue` or `kevent`. It has
`socket`, `bind`, `connect`, `send`, `setsockopt`, `getsockopt`, `inet_addr` and
`close`. It can talk to the device and cannot listen.

The transport class is named `LinuxSockInterface` and its data path was written
around `recvmmsg`, a Linux syscall Darwin does not have. On the macOS build
those calls are gone, the four-argument `BeginDataXfer` is a bare `ret`, and
`FinishDataXfer` logs a failure and closes the socket. So `smOpenNetworkedDevice`
fails while reading calibration data off the device flash, long before anything
streams.

There is a second, independent problem. Darwin has never implemented POSIX
unnamed semaphores. `sem_init` returns -1 with `ENOSYS` and leaves the `sem_t`
untouched. The library calls it eighteen times and discards the result every
time, so every acquisition engine bar one waits on a semaphore that was never
created. That includes the USB I/Q and fast sweep engines, which the vendor lists
as supported on macOS.

## How this fixes it

Three runtime patches. None modifies anything on disk, so the library's code
signature stays valid and there is no re-signing step. There are two backends
that apply them: `sm_transport.py`, in Python and easy to instrument, and
`native/sm_native.c`, a C library for full rate. The Python one tops out near
100 MB/s. Decimation 1 is 800 MB/s and needs the native one.

All ten transport methods are virtual and contiguous in
`__ZTV18LinuxSockInterface`. After `dlopen`, `sm_transport.py` locates that
vtable through `LC_SYMTAB`, works out the load slide from a symbol `dlsym` can
see, makes the page writable and overwrites the ten slots with a Darwin
implementation. Because every method is replaced, the replacement keeps its own
state keyed by the C++ `this` pointer and never has to reproduce the library's
internal object layout.

The four `sem_*` imports go through the GOT, so they are redirected the same
way, to a small native shim backing them with a pthread mutex and condvar.
Only `libsm_api`'s own GOT is touched, so the host process keeps its own
semaphores.

Every command packet passes through the replacement `BeginCommandXfer`, so the
transport recognises `WriteIQFilter` uploads carrying an impulse and substitutes
the shipped coefficients before the packet leaves. Uploads that already carry
real coefficients pass through untouched, so a fixed future build is safe.

## Build and run

```
make
```

That builds `sem_shim.dylib` for the Python backend and
`native/libsmnative.dylib` for the native one. `make test` runs the native
transport against a device simulator on loopback; see `docs/guide.md` for the
one-off loopback alias it needs on macOS.

Extract the decimation filter tables once, from the aarch64 or x86-64 Linux
build shipped in the same SDK. They are Signal Hound's data, so they are read
from your copy at setup time rather than stored here:

```
python3 sm_filters.py extract ./libsm_api.so.2.3.9
```

That writes `filter_tables.json`, which is ignored by git.

Put `filter_tables.json` beside the scripts, then:

```
python3 sm_transport.py ./libsm_api.2.3.7.dylib 192.168.2.2 192.168.2.10 51665
```

That opens the device and reports what happened. For I/Q capture:

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib \
    --center 1e9,2.4e9 --decimation 64 --seconds 2
```

At full rate, 200 MS/s, use the native backend and 16-bit samples. Prove the
link with `--discard` before involving the disk:

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib --native \
    --decimation 1 --short --seconds 10 --discard
```

Each capture ends with a verdict, clean or a list of what was lost, and writes a
JSON sidecar beside the samples with the loss counters and the sample positions
of any holes.

For an unattended battery of experiments writing a JSON and HTML report:

```
python3 sm_diag.py ./libsm_api.2.3.7.dylib --list
python3 sm_diag.py ./libsm_api.2.3.7.dylib --all
python3 sm_diag.py ./libsm_api.2.3.7.dylib --interactive
```

## Host setup

The device defaults to 192.168.2.10 on port 51665. Give the interface a static
address in the same subnet and set the MTU to 9000, since the data datagrams are
8200 bytes and will not fit otherwise. The device ignores ping but answers ARP,
so `ping` followed by `arp -n 192.168.2.10` is a quick liveness check.

The library asks for a 100 MB `SO_RCVBUF` and treats refusal as fatal. macOS
caps it at `kern.ipc.maxsockbuf`, 8 MB on current Apple Silicon Macs. The
sysctl will not go higher (it answers "Result too large"), because the limit is
tied to the mbuf cluster pool and raising that needs the `ncl` boot argument.
Both backends negotiate the request down instead. At 100 MB/s, 8 MB is plenty.
At decimation 1 it holds only 5 to 10 ms of traffic, so the native backend runs
its receiver thread under the real-time scheduling policy audio threads use,
which keeps it from being held off a core for that long.

## Status

Working on hardware: device open, calibration read, command and data transport,
semaphores, sweep and I/Q configuration, retuning, and sustained transfer at
103 MB/s with an unbroken datagram counter. Sweeps return real spectra.

I/Q streaming returned exact zeros while the aux block beside the samples stayed
live. The cause is a third gap in this build. `SmDevice::ConfigureIQStreamingNet`
programs four decimation filter stages in the device using
`DSP::GetLowpassFIRTaps_64f`, which on macOS zeroes the tap array and writes a
single 1.0 at the centre: a unit impulse rather than a low-pass. After
`WriteIQFilter` normalises and scales it, that centre coefficient needs 19 or 20
bits where real filters need 17. Sweep uses a different upload path, which is
why sweeps work and I/Q does not. The Linux builds write these stages from
constant tables; `sm_filters.py` extracts them from your copy and both backends
substitute them into outgoing command packets. On hardware the uploads have now
been seen leaving the library as impulses and being replaced. Whether that
yields real samples is the next thing to confirm.

The first diagnostics runs could not show that. The harness ran
`smNetworkedSpeedTest` before its experiments, the speed test always fails on
the Python transport, and one failure sets a connection-lost status the library
never clears. Every I/Q experiment then failed without capturing anything, and
the report called that zeros. The harness no longer runs it, and now records
that status throughout; see `docs/findings.md`.

To settle whether the filters are the cause, the harness runs a built-in A/B:
`iq-ab-repair-on` and `iq-ab-repair-off`, the same settings with repair on then
off, in one session. It prints a plain verdict comparing the two, so a single
run answers the question rather than two runs into separate directories.

Decimation 1 is built and tested offline, against a simulator that reproduces
the device's wire format, counter behaviour and request-driven flow. Reading the
I/Q engine for it turned up three more things, all handled in the native
backend and described in `docs/findings.md`:

- Any short transfer sets an interface status that nothing on the networked
  path ever clears, after which the engine sleeps 32 ms before every transfer.
  At decimation 1 one lost datagram would starve the device for the rest of the
  session. The native backend zero-fills losses in place, reports the transfer
  complete, and accounts for the loss itself.
- The datagram counter is 16 bits and wraps about 1.5 times a second at
  decimation 1, usually part way through a transfer.
- The engine raises its thread priority with `SCHED_FIFO` at priority 10, a
  Linux-style value. As far as I can tell from the XNU source, on Darwin this
  pins the thread at fixed priority 10, below every normal thread. Not yet confirmed on hardware;
  the native backend records what it finds and corrects it.

Known correctness trap: `DSP::FIR_32fc::SetTaps` discards its taps and
`DSP::FIR_32fc::Filter` is a plain `memcpy` in this build and in the aarch64
Linux build, so the host-side software low-pass that should precede
downsampling does nothing. Any decimation of 16 or higher therefore aliases
instead of filtering. Decimations of 8 and below are hardware only and
unaffected.

## Caveats

The native backend receives one datagram per `recvmsg`, about 97,600 system
calls a second at decimation 1. I expect that to cost around half a performance
core, but it has not been measured on a Mac yet. If it falls short, Darwin's
`recvmsg_x` batches receives; it is private API, so it is not used here.

Offsets are resolved by symbol name rather than hard-coded address, so a minor
library update should still work. A change to the `LinuxSockInterface` class
layout or its virtual method order will not, and would show up as a crash rather
than a quiet wrong answer.

Tested against `libsm_api` 2.3.7 on Apple Silicon.
