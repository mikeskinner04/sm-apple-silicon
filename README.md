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
time, so every acquisition engine waits on a semaphore that was never created.

## How this fixes it

Two runtime patches. Neither modifies anything on disk, so the library's code
signature stays valid and there is no re-signing step.

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

## Build and run

```
clang -dynamiclib -O2 -o sem_shim.dylib sem_shim.c
```

or just `make`.

Put `sem_shim.dylib` beside the scripts, then:

```
python3 sm_transport.py ./libsm_api.2.3.7.dylib 192.168.2.2 192.168.2.10 51665
```

That opens the device and reports what happened. For I/Q capture:

```
python3 sm_iq_capture.py ./libsm_api.2.3.7.dylib \
    --center 1e9,2.4e9 --decimation 64 --seconds 2
```

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

No sysctl tuning is required. The library asks for a 100 MB `SO_RCVBUF` and
treats refusal as fatal, and macOS caps this at `kern.ipc.maxsockbuf`, which is
commonly pinned at 8 MB and rejects larger values with `ERANGE`. This transport
negotiates the request down instead. At 8 MB it has sustained 103 MB/s with no
dropped datagrams.

## Status

Working: device open, calibration read, command and data transport, semaphores,
sweep and I/Q configuration, retuning, and sustained transfer at 103 MB/s with
an unbroken datagram sequence counter.

Open: the sample region of each transfer arrives as zeros while the aux block
beside it carries live data. Rates, datagram counts and buffer arithmetic all
reconcile exactly, so the samples are absent rather than mishandled.
`sm_diag.py` exists to narrow this down in one bench session.

Known correctness trap: `DSP::FIR_32fc::SetTaps` discards its taps and
`DSP::FIR_32fc::Filter` is a plain `memcpy` in this build, so the software
low-pass that should precede downsampling does nothing. Any decimation of 16 or
higher therefore aliases instead of filtering. Decimations of 8 and below are
hardware-only and unaffected.

`docs/guide.md` is the step by step version: host setup, capture, diagnostics,
reading the output and troubleshooting. `docs/findings.md` records the binary
analysis this is built on, including the transport contract, the wire format and
the field offsets.

## Caveats

This is a correctness prototype. The receive loop is Python, one datagram at a
time, which is comfortable to roughly 25 MS/s and nowhere near native 200 MS/s.
Moving the hot path to C with `recvmsg_x` is the obvious next step.

Offsets are resolved by symbol name rather than hard-coded address, so a minor
library update should still work. A change to the `LinuxSockInterface` class
layout or its virtual method order will not, and would show up as a crash rather
than a quiet wrong answer.

Tested against `libsm_api` 2.3.7 on Apple Silicon.
