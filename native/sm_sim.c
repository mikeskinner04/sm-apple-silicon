/* sm_sim.c - stands in for an SM200C on the network, for testing sm_native.
 *
 * Listens on the device address. A command whose first word is 0x7E570001
 * asks for word[1] datagrams, which are sent back in the device's wire format:
 * an 8-byte header whose first word is the counter, then 8192 bytes of payload.
 * Like the real device the counter is 16 bits wide: it wraps from 0xFFFF to 0
 * and the upper half of the word stays put.
 *
 * Payload word 1 is the datagram's true position in the stream plus one (never
 * reset, never wraps, and dropped datagrams still use up a position) and the
 * last word is its complement, so a test can check every datagram landed
 * exactly where it belongs. Like the device, the last datagram of a request of
 * 32 or more is the aux block: its word 0 is the magic "4XU1".
 *
 * Surplus datagrams, extra ones the host never asked for, carry the marker
 * 0xFFFFFFF0 in word 1 and take a fresh counter value, as unrequested traffic
 * from the device would.
 *
 * 0x7E570002 stops the simulator. 0x7E570003 sends word[1] surplus datagrams
 * at once, unrequested. Any other command is optionally appended to a dump
 * file, which is how the filter repair in the send path is verified.
 *
 *   sm_sim ADDR PORT [--drop-every N] [--dup-every N] [--reset-after K]
 *                    [--surplus-after K] [--surplus-count S]
 *                    [--pace-us U] [--seq-start S] [--dump FILE]
 */

#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define CMD_BYTES 2048
#define HDR_BYTES 8
#define PAYLOAD 8192
#define DGRAM (HDR_BYTES + PAYLOAD)

static const uint32_t AUX_MAGIC = 0x31555834;   /* "4XU1" little-endian */
static const uint32_t SURPLUS = 0xFFFFFFF0;

static void send_surplus(int s, uint8_t *out, uint32_t *seq, struct sockaddr_in *peer,
                         socklen_t pl)
{
	uint32_t hdr = 0x5A5A0000u | (*seq & 0xFFFFu), inv = ~SURPLUS, zero = 0;
	memcpy(out, &hdr, 4);
	memset(out + 4, 0, 4);
	memcpy(out + HDR_BYTES, &zero, 4);
	memcpy(out + HDR_BYTES + 4, &SURPLUS, 4);
	memcpy(out + DGRAM - 4, &inv, 4);
	sendto(s, out, DGRAM, 0, (struct sockaddr *)peer, pl);
	(*seq)++;
}

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "usage: sm_sim ADDR PORT [options]\n");
		return 2;
	}
	const char *addr = argv[1];
	int port = atoi(argv[2]);
	long drop_every = 0, dup_every = 0, reset_after = 0, pace_us = 0;
	long surplus_after = -1, surplus_count = 1;
	uint32_t seq = 0x1000;
	const char *dump = NULL;
	for (int i = 3; i + 1 < argc; i += 2) {
		if (!strcmp(argv[i], "--drop-every")) drop_every = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--dup-every")) dup_every = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--reset-after")) reset_after = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--surplus-after")) surplus_after = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--surplus-count")) surplus_count = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--pace-us")) pace_us = atol(argv[i + 1]);
		else if (!strcmp(argv[i], "--seq-start")) seq = (uint32_t)strtoul(argv[i + 1], 0, 0);
		else if (!strcmp(argv[i], "--dump")) dump = argv[i + 1];
	}

	int s = socket(AF_INET, SOCK_DGRAM, 0);
	int big = 64 << 20;
	setsockopt(s, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
	struct sockaddr_in me;
	memset(&me, 0, sizeof(me));
	me.sin_family = AF_INET;
	me.sin_port = htons(port);
	me.sin_addr.s_addr = inet_addr(addr);
	if (bind(s, (struct sockaddr *)&me, sizeof(me)) != 0) {
		perror("bind");
		return 1;
	}
	printf("sim ready on %s:%d\n", addr, port);
	fflush(stdout);

	uint8_t out[DGRAM];
	for (int j = HDR_BYTES; j < DGRAM; j++)
		out[j] = (uint8_t)(j * 131);
	uint8_t cmd[CMD_BYTES];
	long sent_total = 0, dropped = 0, dups = 0, surplus = 0;
	int reset_done = 0;

	for (;;) {
		struct sockaddr_in peer;
		socklen_t pl = sizeof(peer);
		ssize_t n = recvfrom(s, cmd, sizeof(cmd), 0, (struct sockaddr *)&peer, &pl);
		if (n <= 0)
			continue;
		uint32_t w0, w1;
		memcpy(&w0, cmd, 4);
		memcpy(&w1, cmd + 4, 4);
		if (w0 == 0x7E570002)
			break;
		if (w0 == 0x7E570003) {
			for (uint32_t k = 0; k < w1; k++, surplus++)
				send_surplus(s, out, &seq, &peer, pl);
			continue;
		}
		if (w0 != 0x7E570001) {
			if (dump) {
				FILE *fp = fopen(dump, "ab");
				if (fp) {
					fwrite(cmd, 1, (size_t)n, fp);
					fclose(fp);
				}
			}
			continue;
		}
		for (uint32_t k = 0; k < w1; k++) {
			if (reset_after && !reset_done && sent_total == reset_after) {
				seq = 0;
				reset_done = 1;
			}
			uint32_t hdr = 0x5A5A0000u | (seq & 0xFFFFu);
			uint32_t pos = (uint32_t)sent_total + 1, inv = ~pos;
			uint32_t w0p = (w1 >= 32 && k == w1 - 1) ? AUX_MAGIC : pos;
			memcpy(out, &hdr, 4);
			memset(out + 4, 0, 4);
			memcpy(out + HDR_BYTES, &w0p, 4);
			memcpy(out + HDR_BYTES + 4, &pos, 4);
			memcpy(out + DGRAM - 4, &inv, 4);
			int drop = drop_every && (sent_total % drop_every) == drop_every - 1;
			if (!drop) {
				sendto(s, out, DGRAM, 0, (struct sockaddr *)&peer, pl);
				if (dup_every && (sent_total % dup_every) == dup_every - 1) {
					sendto(s, out, DGRAM, 0, (struct sockaddr *)&peer, pl);
					dups++;
				}
			} else {
				dropped++;
			}
			seq++;
			sent_total++;
			if (surplus_after >= 0 && sent_total == surplus_after) {
				for (long j = 0; j < surplus_count; j++, surplus++)
					send_surplus(s, out, &seq, &peer, pl);
			}
			if (pace_us && (sent_total % 16) == 0)
				usleep((useconds_t)pace_us);
		}
	}
	printf("sim sent %ld dropped %ld duplicated %ld surplus %ld\n", sent_total, dropped, dups, surplus);
	return 0;
}
