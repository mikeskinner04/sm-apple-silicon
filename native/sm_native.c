/* sm_native.c - native replacement transport for libsm_api on macOS.
 *
 * Built for sustained I/Q at decimation 1: about 800 MB/s, roughly 97,600
 * datagrams a second. Replaces the Python transport in sm_transport.py, which
 * tops out near 100 MB/s, and folds in the semaphore shim and the decimation
 * filter repair so one library covers all three runtime fixes.
 *
 * How the library drives it (from EngineIQStreamingNetworked)
 *
 * Streaming is request driven. The engine arms a transfer, then sends a request
 * command for it, and keeps between 2 and 16 such requests outstanding
 * (smSetIQQueueSize / 2.62 ms). A transfer is 2 MB divided by the hardware
 * decimation, so 256 datagrams at decimation 1, and the device sends nothing it
 * was not asked for. At most 16 x 2 MB is ever in flight.
 *
 * Design
 *
 * A dedicated receiver thread per interface drains the socket and scatters
 * each datagram straight into the armed slot buffer: header to a stack array,
 * payload to its final position. Slot buffers are allocated once and
 * pre-faulted so the hot path never takes a page fault.
 *
 * Datagrams are placed by sequence number, not arrival count. The device
 * counter is 16 bits wide on the wire (it wraps from 0xFFFF to 0 with the upper
 * half unchanged, about 1.5 times a second at decimation 1), so all sequence
 * arithmetic is modulo 65536. A transfer's base is the previous transfer's base
 * plus its datagram count, so a drop leaves a zero-filled hole in one transfer
 * and the next still lands in the right place.
 *
 * Loss policy
 *
 * The engine treats any short transfer by setting the interface status to -6,
 * which nothing on the networked path ever clears, and then sleeping 32 ms
 * before every later transfer. At decimation 1 that is 12 times longer than a
 * transfer lasts, so one lost datagram would starve the device for the rest of
 * the session. The engine stores the full buffer either way. So for stream
 * transfers (32 datagrams or more) lost datagrams are zero-filled, the
 * transfer is reported complete, and the loss is counted here instead. A stream
 * transfer that received nothing at all before the timeout is still reported as
 * timed out. Smaller transfers, the calibration and flash reads at open, keep
 * the original strict behaviour so a damaged read fails the open rather than
 * loading bad calibration.
 *
 * Every method below has the ABI of the C++ virtual it replaces: `this` arrives
 * as the first argument, exactly as a plain C function receives it on arm64.
 *
 *   macOS:  clang -dynamiclib -O2 -o libsmnative.dylib sm_native.c
 *   Linux:  gcc -shared -fPIC -O2 -pthread -o libsmnative.so sm_native.c  (tests)
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <pthread/qos.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/thread_act.h>
#include <mach/thread_policy.h>
#endif

#define CMD_BYTES    2048
#define CMD_WORDS    (CMD_BYTES / 4)
#define HDR_BYTES    8
#define PAYLOAD      8192
#define MAX_MSGS     256
#define SLOTS        32
#define SLOT_BYTES   ((size_t)MAX_MSGS * PAYLOAD)
#define RCVBUF_WANT  100000000
#define FD_OFFSET    0x10      /* the original keeps its socket here; its dtor closes it */
#define ERR_OFFSET   0x08      /* error byte read by DeviceInterfaceNetworked */
#define MAX_IFACES   16
#define FIFO_LEN     64        /* power of two, larger than SLOTS */
#define POLL_MS      100       /* receive timeout granularity, bounds shutdown latency */
#define STREAM_MIN_MSGS 32     /* smallest I/Q stream transfer: 256 KB at hardware decimation 8 */
#define SEQ_WINDOW   32        /* transfers; a jump beyond this many is a counter restart */
#define STRAY_RUN    8         /* this many consecutive stale datagrams is also a restart */
#define LIB_PRIORITY 47        /* timeshare priority for library threads, as user-interactive QoS */

typedef struct {
	uint64_t datagrams;        /* placed into a transfer */
	uint64_t payload_bytes;
	uint64_t lost;             /* datagrams missing by sequence */
	uint64_t gaps;             /* loss events */
	uint64_t resets;           /* device counter restarts */
	uint64_t strays;           /* duplicates or datagrams behind the write position */
	uint64_t short_datagrams;
	uint64_t timeouts;
	uint64_t transfers;
	uint64_t short_transfers;  /* completed with fewer bytes than requested */
	uint64_t filter_repairs;
	uint64_t commands;
	uint64_t aux_lost;         /* stream transfers whose final (aux) datagram was lost */
	uint64_t queue_empty;      /* stream transfer completed with nothing else armed */
	uint64_t lib_promotions;   /* library threads moved to LIB_PRIORITY (macOS) */
	uint64_t max_outstanding;  /* armed transfers waiting, high water */
	int32_t rcvbuf;            /* negotiated SO_RCVBUF */
	int32_t rx_sched;          /* receiver thread: 2 real-time, 1 user-interactive QoS, 0 default */
	int32_t lib_policy_low;    /* lowest-priority library thread seen: Mach policy */
	int32_t lib_prio_low;      /* ... and its base priority, 0 if none seen */
	int32_t lib_policy_after;  /* after promotion */
	int32_t lib_prio_after;
} smn_stats_t;

typedef struct {
	bool valid;
	uint16_t seq;
	uint8_t payload[PAYLOAD];
} carry_t;

typedef struct {
	void *owner;               /* the C++ LinuxSockInterface */
	int sock;
	uint8_t *mem;              /* SLOTS * SLOT_BYTES, allocated once */
	int requested[SLOTS];
	int nmsgs[SLOTS];
	int received[SLOTS];
	bool timed_out[SLOTS];
	bool done[SLOTS];
	int cmd_sent[SLOTS];
	int fifo[FIFO_LEN];
	unsigned fifo_head, fifo_tail;
	pthread_mutex_t mu;
	pthread_cond_t armed, completed;
	pthread_t rx;
	bool rx_started;
	volatile bool stop;
	/* receiver thread only */
	bool have_next;
	uint16_t next_seq;
	int stray_run;
	uint16_t stray_seq0;       /* first datagram of the current stale run */
	int stray_idx0;            /* ... and the write position it arrived at */
	carry_t carry;
	smn_stats_t st;
} iface_t;

static iface_t *ifaces[MAX_IFACES];
static pthread_mutex_t ifaces_mu = PTHREAD_MUTEX_INITIALIZER;
static int timeout_ms = 2000;  /* silence before a transfer is declared timed out */

/* filter repair tables, indexed by tap count */
static double *filter_table[256];
static const struct { int taps; uint32_t scale; uint16_t addr; } stages[] = {
	{189, 1u << 19, 0x478}, {79, 1u << 18, 0x578},
	{159, 1u << 18, 0x678}, {159, 1u << 18, 0x778},
};

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static uint32_t le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

static iface_t *get_iface(void *owner, bool create)
{
	pthread_mutex_lock(&ifaces_mu);
	iface_t *found = NULL;
	for (int i = 0; i < MAX_IFACES && !found; i++)
		if (ifaces[i] && ifaces[i]->owner == owner)
			found = ifaces[i];
	if (!found && create) {
		for (int i = 0; i < MAX_IFACES; i++) {
			if (!ifaces[i]) {
				found = calloc(1, sizeof(iface_t));
				found->owner = owner;
				found->sock = -1;
				pthread_mutex_init(&found->mu, NULL);
				pthread_cond_init(&found->armed, NULL);
				pthread_cond_init(&found->completed, NULL);
				ifaces[i] = found;
				break;
			}
		}
	}
	pthread_mutex_unlock(&ifaces_mu);
	return found;
}

/* ---- receive path --------------------------------------------------------- */

typedef struct {
	int slot, n;
	uint8_t *buf;
	int placed;       /* datagrams stored */
	int next_idx;     /* position the next in-order datagram belongs at */
	bool have_base;
	uint16_t base;
} xfer_t;

/* Place one datagram. `data` is where its payload currently sits: in place at
 * next_idx if it was received there, or in the carry buffer. Returns 1 if the
 * datagram belongs to a later transfer and must be carried over.
 *
 * d is the signed 16-bit distance from the sequence number expected at the
 * write position. A small negative d is a duplicate or straggler and is
 * dropped. A forward d inside the window is loss. Anything else cannot be
 * explained by loss, because the device never has more than 16 transfers in
 * flight, so the counter restarted and placement falls back to arrival order. */
static int accept_dgram(iface_t *f, xfer_t *x, uint16_t seq, const uint8_t *data)
{
	if (!x->have_base) {
		x->base = f->have_next ? f->next_seq : seq;
		x->have_base = true;
	}
	int window = SEQ_WINDOW * x->n;
	if (window > 16384)
		window = 16384;
	int d = (int16_t)(uint16_t)(seq - (uint16_t)(x->base + x->next_idx));
	bool restart = d >= window || d <= -window;
	if (d < 0 && !restart) {
		if (f->stray_run++ == 0) {
			f->stray_seq0 = seq;
			f->stray_idx0 = x->next_idx;
		}
		if (f->stray_run < STRAY_RUN) {
			f->st.strays++;
			return 0;
		}
		/* A run of stale datagrams is a restart that began at the first of
		 * them. Anchor there, so the run becomes a counted hole and everything
		 * after it keeps its place in the transfer. */
		f->st.resets++;
		f->st.strays -= (uint64_t)(STRAY_RUN - 1);
		x->base = (uint16_t)(f->stray_seq0 - f->stray_idx0);
		d = (int16_t)(uint16_t)(seq - (uint16_t)(x->base + x->next_idx));
	} else if (restart) {
		f->st.resets++;                     /* restarted onto this datagram */
		x->base = (uint16_t)(seq - x->next_idx);
		d = 0;
	}
	f->stray_run = 0;
	int idx = x->next_idx + d;
	if (idx >= x->n)
		return 1;
	uint8_t *dst = x->buf + (size_t)idx * PAYLOAD;
	if (data != dst)
		memmove(dst, data, PAYLOAD);
	if (d > 0) {
		memset(x->buf + (size_t)x->next_idx * PAYLOAD, 0, (size_t)d * PAYLOAD);
		f->st.lost += (uint64_t)d;
		f->st.gaps++;
	}
	x->placed++;
	x->next_idx = idx + 1;
	f->st.datagrams++;
	f->st.payload_bytes += PAYLOAD;
	return 0;
}

static void complete_xfer(iface_t *f, xfer_t *x, bool timed_out)
{
	bool stream = x->n >= STREAM_MIN_MSGS;
	if (x->next_idx < x->n) {
		uint32_t missing = (uint32_t)(x->n - x->next_idx);
		memset(x->buf + (size_t)x->next_idx * PAYLOAD, 0, (size_t)missing * PAYLOAD);
		if (x->have_base) {                  /* only count what was sent and lost */
			f->st.lost += missing;
			f->st.gaps++;
		}
		if (stream)
			f->st.aux_lost++;                /* the last datagram carries the aux block */
	}
	if (x->have_base) {
		f->next_seq = (uint16_t)(x->base + x->n);
		f->have_next = true;
	}
	f->st.transfers++;
	if (x->placed < x->n)
		f->st.short_transfers++;
	if (timed_out)
		f->st.timeouts++;

	/* See the loss policy at the top of the file. */
	int received = x->placed * PAYLOAD;
	bool report_timeout = timed_out;
	if (stream && x->placed > 0) {
		received = x->n * PAYLOAD;
		report_timeout = false;
	} else if (stream && !timed_out) {
		received = x->n * PAYLOAD;           /* lost whole, but the stream moved on */
	}

	pthread_mutex_lock(&f->mu);
	f->received[x->slot] = received;
	f->timed_out[x->slot] = report_timeout;
	f->done[x->slot] = true;
	f->fifo_head++;
	if (stream && f->fifo_head == f->fifo_tail)
		f->st.queue_empty++;
	pthread_cond_broadcast(&f->completed);
	pthread_mutex_unlock(&f->mu);
}

/* The kernel buffer is capped at 8 MB, which holds only 5 to 10 ms of
 * decimation-1 traffic, so the receiver must never be kept off a core for
 * longer than that. Ask for the time-constraint policy audio threads use: it
 * outranks every timeshare thread. The thread blocks in recvmsg between bursts,
 * so it never runs long enough to trip the scheduler's real-time failsafe. If
 * the kernel refuses, or SMN_NO_REALTIME is set, fall back to user-interactive
 * QoS. Returns 2 for real-time, 1 for QoS, 0 for neither. */
static int raise_rx_thread(void)
{
#ifdef __APPLE__
	if (!getenv("SMN_NO_REALTIME")) {
		mach_timebase_info_data_t tb;
		mach_timebase_info(&tb);
		double per_us = 1000.0 * tb.denom / tb.numer;  /* abs time units per us */
		thread_time_constraint_policy_data_t p;
		p.period = (uint32_t)(2621 * per_us);          /* one 2 MB transfer at 800 MB/s */
		p.computation = (uint32_t)(1300 * per_us);
		p.constraint = (uint32_t)(2200 * per_us);
		p.preemptible = TRUE;
		if (thread_policy_set(pthread_mach_thread_np(pthread_self()),
		                      THREAD_TIME_CONSTRAINT_POLICY, (thread_policy_t)&p,
		                      THREAD_TIME_CONSTRAINT_POLICY_COUNT) == KERN_SUCCESS)
			return 2;
	}
	return pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0) == 0;
#else
	return 0;
#endif
}

static void *rx_main(void *arg)
{
	iface_t *f = arg;
	f->st.rx_sched = raise_rx_thread();
	uint8_t hdr[HDR_BYTES];

	for (;;) {
		pthread_mutex_lock(&f->mu);
		while (f->fifo_head == f->fifo_tail && !f->stop)
			pthread_cond_wait(&f->armed, &f->mu);
		if (f->stop) {
			pthread_mutex_unlock(&f->mu);
			break;
		}
		xfer_t x = {0};
		x.slot = f->fifo[f->fifo_head % FIFO_LEN];
		x.n = f->nmsgs[x.slot];
		pthread_mutex_unlock(&f->mu);
		x.buf = f->mem + (size_t)x.slot * SLOT_BYTES;

		bool timed_out = false, ended = false;

		if (f->carry.valid) {
			if (accept_dgram(f, &x, f->carry.seq, f->carry.payload) == 0)
				f->carry.valid = false;
			else
				ended = true;                /* whole transfer missing */
		}

		double last = now_s();
		while (!ended && x.next_idx < x.n) {
			struct iovec iov[2] = {
				{hdr, HDR_BYTES},
				{x.buf + (size_t)x.next_idx * PAYLOAD, PAYLOAD},
			};
			struct msghdr mh;
			memset(&mh, 0, sizeof(mh));
			mh.msg_iov = iov;
			mh.msg_iovlen = 2;
			ssize_t got = recvmsg(f->sock, &mh, 0);
			if (got < 0) {
				if (f->stop)
					break;
				if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
					if ((now_s() - last) * 1000.0 > timeout_ms) {
						timed_out = true;
						break;
					}
					continue;
				}
				timed_out = true;            /* socket closed under us */
				break;
			}
			last = now_s();
			if (got != HDR_BYTES + PAYLOAD)
				f->st.short_datagrams++;
			uint16_t seq = (uint16_t)le32(hdr);  /* low half is the counter */
			uint8_t *landed = x.buf + (size_t)x.next_idx * PAYLOAD;
			if (accept_dgram(f, &x, seq, landed) == 1) {
				memcpy(f->carry.payload, landed, PAYLOAD);
				f->carry.seq = seq;
				f->carry.valid = true;
				ended = true;
			}
		}
		if (f->stop)
			break;
		complete_xfer(f, &x, timed_out);
	}
	return NULL;
}

/* ---- library thread priority (macOS) ---------------------------------------
 *
 * The engine calls SetThreadPriorityTimeCritical on its transfer thread, which
 * is pthread_setschedparam(policy 4, priority 10). Policy 4 is SCHED_FIFO on
 * Darwin, and XNU takes 10 as an absolute priority, so as far as I can tell
 * from the XNU source this pins the thread at fixed priority 10: below every
 * default thread (31) and in the background band the scheduler steers to the
 * efficiency cores. Not verified on hardware; the statistics record what the
 * thread actually had, so one run settles it.
 *
 * finish_data runs on the engine's thread, so it checks the caller now and then
 * and, unless disabled, moves it to timeshare priority 47, the same band as
 * user-interactive QoS. */

static int promote_lib_threads = 1;
static struct {
	uint64_t promotions;
	int32_t policy_low, prio_low, policy_after, prio_after;
} libdiag;

#ifdef __APPLE__
static void check_caller_priority(void)
{
	static __thread unsigned calls;
	if ((calls++ & 63) != 0)
		return;
	mach_port_t t = pthread_mach_thread_np(pthread_self());
	thread_extended_info_data_t info;
	mach_msg_type_number_t cnt = THREAD_EXTENDED_INFO_COUNT;
	if (thread_info(t, THREAD_EXTENDED_INFO, (thread_info_t)&info, &cnt) != KERN_SUCCESS)
		return;
	if (info.pth_policy == POLICY_TIMESHARE && info.pth_priority >= LIB_PRIORITY)
		return;
	if (libdiag.prio_low == 0 || info.pth_priority < libdiag.prio_low) {
		libdiag.policy_low = info.pth_policy;
		libdiag.prio_low = info.pth_priority;
	}
	if (!promote_lib_threads)
		return;
	struct sched_param sp = {.sched_priority = LIB_PRIORITY};
	if (pthread_setschedparam(pthread_self(), SCHED_OTHER, &sp) != 0)
		return;
	libdiag.promotions++;
	cnt = THREAD_EXTENDED_INFO_COUNT;
	if (thread_info(t, THREAD_EXTENDED_INFO, (thread_info_t)&info, &cnt) == KERN_SUCCESS) {
		libdiag.policy_after = info.pth_policy;
		libdiag.prio_after = info.pth_priority;
	}
}
#else
static void check_caller_priority(void) {}
#endif

void smn_set_promote(int on) { promote_lib_threads = on != 0; }

/* ---- the ten replaced methods --------------------------------------------- */

static void stop_rx(iface_t *f)
{
	if (!f->rx_started)
		return;
	pthread_mutex_lock(&f->mu);
	f->stop = true;
	pthread_cond_broadcast(&f->armed);
	pthread_cond_broadcast(&f->completed);
	pthread_mutex_unlock(&f->mu);
	pthread_join(f->rx, NULL);
	f->rx_started = false;
}

bool smn_allocate(void *self, const char *host, const char *dev, uint16_t port)
{
	iface_t *f = get_iface(self, true);
	if (!f)
		return false;
	stop_rx(f);
	if (f->sock >= 0) {
		close(f->sock);
		f->sock = -1;
	}

	int s = socket(AF_INET, SOCK_DGRAM, 0);
	if (s < 0) {
		fprintf(stderr, "sm_native: socket: %s\n", strerror(errno));
		return false;
	}
	int one = 1;
	setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	/* macOS refuses a request above kern.ipc.maxsockbuf rather than clamping
	 * it, so after the full ask try powers of two downwards. That limit is
	 * bounded by the mbuf cluster pool and on current Apple Silicon Macs will
	 * not go above 8 MB without the ncl boot argument, so expect 8 MB. */
	int want = RCVBUF_WANT;
	if (setsockopt(s, SOL_SOCKET, SO_RCVBUF, &want, sizeof(want)) != 0)
		for (want = 1 << 27; want >= (1 << 20); want >>= 1)
			if (setsockopt(s, SOL_SOCKET, SO_RCVBUF, &want, sizeof(want)) == 0)
				break;
	int got = 0;
	socklen_t gl = sizeof(got);
	getsockopt(s, SOL_SOCKET, SO_RCVBUF, &got, &gl);
	struct timeval tv = {0, POLL_MS * 1000};
	setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

	struct sockaddr_in a;
	memset(&a, 0, sizeof(a));
	a.sin_family = AF_INET;
	a.sin_port = htons(port);
	a.sin_addr.s_addr = inet_addr(host);
	if (bind(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
		fprintf(stderr, "sm_native: bind %s:%u: %s\n", host, port, strerror(errno));
		close(s);
		return false;
	}
	a.sin_addr.s_addr = inet_addr(dev);
	if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
		fprintf(stderr, "sm_native: connect %s:%u: %s\n", dev, port, strerror(errno));
		close(s);
		return false;
	}

	if (!f->mem) {
		size_t bytes = (size_t)SLOTS * SLOT_BYTES;
		void *m = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
		if (m == MAP_FAILED) {
			fprintf(stderr, "sm_native: mmap %zu bytes: %s\n", bytes, strerror(errno));
			close(s);
			return false;
		}
		memset(m, 0, bytes);                 /* pre-fault every page */
		mlock(m, bytes);                     /* best effort */
		f->mem = m;
	}

	memset(&f->st, 0, sizeof(f->st));
	f->st.rcvbuf = got;
	memset(f->done, 0, sizeof(f->done));
	memset(f->nmsgs, 0, sizeof(f->nmsgs));
	f->fifo_head = f->fifo_tail = 0;
	f->have_next = false;
	f->stray_run = 0;
	f->carry.valid = false;
	f->sock = s;
	f->stop = false;
	int perr = pthread_create(&f->rx, NULL, rx_main, f);
	if (perr != 0) {
		fprintf(stderr, "sm_native: pthread_create: %s\n", strerror(perr));
		close(s);
		f->sock = -1;
		return false;
	}
	f->rx_started = true;
	*(int *)((uint8_t *)self + FD_OFFSET) = s;
	return true;
}

void smn_deallocate(void *self)
{
	iface_t *f = get_iface(self, false);
	if (!f)
		return;
	stop_rx(f);
	if (f->sock >= 0) {
		close(f->sock);
		f->sock = -1;
	}
	*(int *)((uint8_t *)self + FD_OFFSET) = -1;  /* so the original dtor closes nothing */
	/* Slot memory is kept and reused by a later AllocateResources on the same
	 * object. Freeing it here would risk a use after free if the library reads
	 * Data() after disconnecting. */
}

int smn_repair_packet(uint32_t *w, int nwords);

void smn_begin_cmd(void *self, int idx, const uint8_t *cmd)
{
	iface_t *f = get_iface(self, false);
	if (!f || f->sock < 0 || idx < 0 || idx >= SLOTS)
		return;
	uint32_t words[CMD_WORDS];
	memcpy(words, cmd, CMD_BYTES);
	f->st.filter_repairs += (uint64_t)smn_repair_packet(words, CMD_WORDS);
	f->st.commands++;
	ssize_t n = send(f->sock, words, CMD_BYTES, 0);
	f->cmd_sent[idx] = n < 0 ? 0 : (int)n;
	if (n != CMD_BYTES)
		*((uint8_t *)self + ERR_OFFSET) = 1;
}

int smn_finish_cmd(void *self, int idx)
{
	iface_t *f = get_iface(self, false);
	if (!f || idx < 0 || idx >= SLOTS)
		return 0;
	int n = f->cmd_sent[idx];
	f->cmd_sent[idx] = 0;
	return n;
}

void smn_begin_data(void *self, int idx, int len, int arg3)
{
	(void)arg3;
	iface_t *f = get_iface(self, false);
	if (!f || idx < 0 || idx >= SLOTS)
		return;
	int n = len / PAYLOAD;
	if (n > MAX_MSGS)
		n = MAX_MSGS;                        /* the original's buffers are this size too */
	pthread_mutex_lock(&f->mu);
	f->requested[idx] = len;
	f->nmsgs[idx] = n;
	f->received[idx] = 0;
	f->timed_out[idx] = false;
	f->done[idx] = n <= 0;
	if (n > 0) {
		f->fifo[f->fifo_tail % FIFO_LEN] = idx;
		f->fifo_tail++;
		uint64_t depth = f->fifo_tail - f->fifo_head;
		if (depth > f->st.max_outstanding)
			f->st.max_outstanding = depth;
		pthread_cond_signal(&f->armed);
	}
	pthread_mutex_unlock(&f->mu);
}

void smn_begin_data_buf(void *self, int idx, uint8_t *buf, int len, int arg3)
{
	(void)buf;                               /* a bare ret in the original */
	smn_begin_data(self, idx, len, arg3);
}

int smn_finish_data(void *self, int idx)
{
	check_caller_priority();
	iface_t *f = get_iface(self, false);
	if (!f || !f->rx_started || idx < 0 || idx >= SLOTS)
		return 0;
	pthread_mutex_lock(&f->mu);
	if (f->nmsgs[idx] <= 0) {
		pthread_mutex_unlock(&f->mu);
		return 0;
	}
	while (!f->done[idx] && !f->stop)
		pthread_cond_wait(&f->completed, &f->mu);
	int r = f->received[idx];
	pthread_mutex_unlock(&f->mu);
	return r;
}

const uint8_t *smn_data(void *self, int idx)
{
	iface_t *f = get_iface(self, false);
	if (!f || !f->mem || idx < 0 || idx >= SLOTS)
		return NULL;
	return f->mem + (size_t)idx * SLOT_BYTES;
}

bool smn_timed_out(void *self, int idx)
{
	iface_t *f = get_iface(self, false);
	return f && idx >= 0 && idx < SLOTS && f->timed_out[idx];
}

int smn_xfer_len(void *self, int idx)
{
	iface_t *f = get_iface(self, false);
	return (f && idx >= 0 && idx < SLOTS) ? f->requested[idx] : 0;
}

/* ---- filter repair -------------------------------------------------------- */

void smn_set_filter_table(int taps, const double *coeffs)
{
	if (taps <= 0 || taps >= 256)
		return;
	free(filter_table[taps]);
	filter_table[taps] = malloc(sizeof(double) * taps);
	memcpy(filter_table[taps], coeffs, sizeof(double) * taps);
}

/* Replace WriteIQFilter uploads carrying a unit impulse with the shipped
 * coefficients. Layout: header (0x04 << 24 | words << 16 | addr), eight zero
 * words, then (n + 1) / 2 int32 coefficients, edge to centre inclusive.
 * Anything that is not an impulse passes through. Returns repairs made. */
int smn_repair_packet(uint32_t *w, int nwords)
{
	int repairs = 0;
	for (int i = 0; i < nwords; i++) {
		if ((w[i] >> 24) != 0x04)
			continue;
		int st = -1;
		for (int s = 0; s < 4; s++)
			if ((w[i] & 0xFFFF) == stages[s].addr)
				st = s;
		if (st < 0)
			continue;
		int n = stages[st].taps, half = (n + 1) / 2;
		uint32_t scale = stages[st].scale;
		if ((int)((w[i] >> 16) & 0xFF) != half + 8)
			continue;
		int start = i + 9, end = start + half;
		if (end > nwords)
			continue;
		bool zero_block = true;
		for (int k = i + 1; k < start; k++)
			if (w[k])
				zero_block = false;
		if (!zero_block)
			continue;
		bool impulse = w[end - 1] == scale;
		for (int k = start; k < end - 1 && impulse; k++)
			if (w[k])
				impulse = false;
		if (impulse && filter_table[n]) {
			double sum = 0;
			for (int k = 0; k < n; k++)
				sum += filter_table[n][k];
			for (int k = 0; k < half; k++)
				w[start + k] = (uint32_t)(int32_t)(filter_table[n][k] / sum * scale);
			repairs++;
		}
		i = end - 1;
	}
	return repairs;
}

/* ---- semaphores ------------------------------------------------------------ */
/* Darwin never implemented unnamed POSIX semaphores; sem_init fails with ENOSYS.
 * sem_t is a four-byte int there, so store a table index in it. Mutex and condvar
 * rather than dispatch, which aborts when a semaphore is disposed with its count
 * below the initial value. */

#define MAX_SEMS 512
typedef struct {
	pthread_mutex_t mu;
	pthread_cond_t cv;
	unsigned value;
	int in_use, ready;
} sem_slot_t;
static sem_slot_t sems[MAX_SEMS];
static pthread_mutex_t sems_mu = PTHREAD_MUTEX_INITIALIZER;

static sem_slot_t *sem_of(int *sem)
{
	int i = *sem;
	return (i >= 0 && i < MAX_SEMS && sems[i].in_use) ? &sems[i] : NULL;
}

int smn_sem_init(int *sem, int pshared, unsigned value)
{
	(void)pshared;
	pthread_mutex_lock(&sems_mu);
	for (int i = 0; i < MAX_SEMS; i++) {
		if (sems[i].in_use)
			continue;
		if (!sems[i].ready) {
			pthread_mutex_init(&sems[i].mu, NULL);
			pthread_cond_init(&sems[i].cv, NULL);
			sems[i].ready = 1;
		}
		sems[i].value = value;
		sems[i].in_use = 1;
		pthread_mutex_unlock(&sems_mu);
		*sem = i;
		return 0;
	}
	pthread_mutex_unlock(&sems_mu);
	errno = ENOSPC;
	return -1;
}

int smn_sem_wait(int *sem)
{
	sem_slot_t *s = sem_of(sem);
	if (!s) {
		errno = EINVAL;
		return -1;
	}
	pthread_mutex_lock(&s->mu);
	while (s->value == 0)
		pthread_cond_wait(&s->cv, &s->mu);
	s->value--;
	pthread_mutex_unlock(&s->mu);
	return 0;
}

int smn_sem_post(int *sem)
{
	sem_slot_t *s = sem_of(sem);
	if (!s) {
		errno = EINVAL;
		return -1;
	}
	pthread_mutex_lock(&s->mu);
	s->value++;
	pthread_cond_signal(&s->cv);
	pthread_mutex_unlock(&s->mu);
	return 0;
}

int smn_sem_destroy(int *sem)
{
	pthread_mutex_lock(&sems_mu);
	sem_slot_t *s = sem_of(sem);
	if (s)
		s->in_use = 0;
	pthread_mutex_unlock(&sems_mu);
	*sem = -1;
	return 0;
}

/* ---- control and statistics ------------------------------------------------ */

void smn_set_timeout_ms(int ms)
{
	if (ms > 0)
		timeout_ms = ms;
}

/* Sum statistics over every interface, or one if owner is not NULL. */
void smn_get_stats(void *owner, smn_stats_t *out)
{
	memset(out, 0, sizeof(*out));
	pthread_mutex_lock(&ifaces_mu);
	for (int i = 0; i < MAX_IFACES; i++) {
		iface_t *f = ifaces[i];
		if (!f || (owner && f->owner != owner))
			continue;
		uint64_t *dst = (uint64_t *)out;
		const uint64_t *src = (const uint64_t *)&f->st;
		for (size_t k = 0; k < offsetof(smn_stats_t, max_outstanding) / 8; k++)
			dst[k] += src[k];
		if (f->st.max_outstanding > out->max_outstanding)
			out->max_outstanding = f->st.max_outstanding;
		out->rcvbuf = f->st.rcvbuf;
		out->rx_sched = f->st.rx_sched;
	}
	pthread_mutex_unlock(&ifaces_mu);
	out->lib_promotions = libdiag.promotions;
	out->lib_policy_low = libdiag.policy_low;
	out->lib_prio_low = libdiag.prio_low;
	out->lib_policy_after = libdiag.policy_after;
	out->lib_prio_after = libdiag.prio_after;
}

int smn_stats_size(void) { return (int)sizeof(smn_stats_t); }
const char *smn_version(void) { return "sm_native 1.1"; }
