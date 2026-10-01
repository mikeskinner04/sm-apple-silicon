/* ref_filters.c - reference I/Q decimation filters from the Linux aarch64 SM API.
 *
 * SmDevice::ConfigureIQStreamingNet designs each of the four decimation filter
 * stages at run time with DSP::GetLowpassFIRTaps_64f and uploads them with
 * CommandList::WriteIQFilter. On macOS the design function is a placeholder
 * that returns a unit impulse. This program calls the real ones inside the
 * Linux aarch64 build, so the macOS repair can be checked against exactly what
 * Signal Hound's working build would send.
 *
 * Both functions are in the library's symbol table but not exported, so they
 * are found the same way sm_transport.py finds the vtable: read the symbol's
 * file address from .symtab and add the load slide, worked out from an
 * exported symbol (smGetAPIVersion).
 *
 * Output is JSON on stdout. Doubles are written as their exact 64-bit
 * patterns so comparisons can be bit for bit.
 *
 *   ref_filters LIBSM_SO [FC ...]
 *
 * With no FC arguments it covers the cutoffs the SM API itself uses plus a
 * sweep from 0.02 to 0.25 in steps of 0.001. Needs an aarch64 Linux host or
 * container; see Dockerfile.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <elf.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CMD_BYTES 2048
#define WIN_BLACKMAN 1          /* the window ConfigureIQStreamingNet passes */

static const struct { int stage, taps; } STAGES[] = {
	{1, 189}, {2, 79}, {3, 159}, {4, 159},
};

/* Cutoffs the library itself uses as defaults and clamps; the same in the
 * macOS 2.3.7 and Linux 2.3.9 builds. Cycles per stage input sample. */
static const double KNOWN_FC[] = {
	0.02, 0.08, 0.083325, 0.2, 0.225,
};

static const char *SYM_ANCHOR = "smGetAPIVersion";
static const char *SYM_DESIGN = "_ZN3DSP21GetLowpassFIRTaps_64fEPdifNS_7WinTypeEb";
static const char *SYM_CTOR = "_ZN11CommandListC1Ev";
static const char *SYM_DTOR = "_ZN11CommandListD1Ev";
static const char *SYM_WRITE = "_ZN11CommandList13WriteIQFilterEiRKSt6vectorIdSaIdEE";
static const char *SYM_CMDPTR = "_ZN11CommandList13GetCommandPtrEi";

typedef void (*design_fn)(double *, int, float, int, bool);
typedef void (*ctor_fn)(void *);
typedef void (*write_fn)(void *, int, const void *);
typedef uint8_t *(*cmdptr_fn)(void *, int);

/* libstdc++ std::vector<double>: begin, end, end of storage. */
typedef struct { double *b, *e, *cap; } vec_t;

/* File address of each wanted symbol, from .symtab. */
static int symtab_lookup(const char *path, const char **names, uint64_t *out, int count)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		return -1;
	fseek(f, 0, SEEK_END);
	long len = ftell(f);
	fseek(f, 0, SEEK_SET);
	uint8_t *d = malloc((size_t)len);
	if (!d || fread(d, 1, (size_t)len, f) != (size_t)len) {
		fclose(f);
		free(d);
		return -1;
	}
	fclose(f);
	Elf64_Ehdr *eh = (Elf64_Ehdr *)d;
	Elf64_Shdr *sh = (Elf64_Shdr *)(d + eh->e_shoff);
	int found = 0;
	for (int i = 0; i < eh->e_shnum; i++) {
		if (sh[i].sh_type != SHT_SYMTAB)
			continue;
		Elf64_Sym *sym = (Elf64_Sym *)(d + sh[i].sh_offset);
		const char *str = (const char *)(d + sh[sh[i].sh_link].sh_offset);
		size_t n = sh[i].sh_size / sizeof(Elf64_Sym);
		for (size_t k = 0; k < n; k++) {
			if (!sym[k].st_value)
				continue;
			for (int j = 0; j < count; j++) {
				if (!out[j] && strcmp(str + sym[k].st_name, names[j]) == 0) {
					out[j] = sym[k].st_value;
					found++;
				}
			}
		}
	}
	free(d);
	return found;
}

static void print_hex_doubles(const double *v, int n)
{
	printf("[");
	for (int i = 0; i < n; i++) {
		uint64_t u;
		memcpy(&u, &v[i], 8);
		printf("%s\"%016" PRIx64 "\"", i ? "," : "", u);
	}
	printf("]");
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s LIBSM_SO [FC ...]\n", argv[0]);
		return 2;
	}
	const char *names[] = {SYM_ANCHOR, SYM_DESIGN, SYM_CTOR, SYM_DTOR, SYM_WRITE, SYM_CMDPTR};
	enum { ANCHOR, DESIGN, CTOR, DTOR, WRITE, CMDPTR, NSYM };
	uint64_t file_addr[NSYM] = {0};
	if (symtab_lookup(argv[1], names, file_addr, NSYM) != NSYM) {
		fprintf(stderr, "could not find every symbol in %s; is it the Linux aarch64 SM API?\n",
		        argv[1]);
		return 1;
	}
	void *lib = dlopen(argv[1], RTLD_NOW);
	if (!lib) {
		fprintf(stderr, "dlopen: %s\n", dlerror());
		return 1;
	}
	uintptr_t anchor = (uintptr_t)dlsym(lib, SYM_ANCHOR);
	if (!anchor) {
		fprintf(stderr, "dlsym %s failed\n", SYM_ANCHOR);
		return 1;
	}
	uintptr_t slide = anchor - file_addr[ANCHOR];
	design_fn design = (design_fn)(slide + file_addr[DESIGN]);
	ctor_fn ctor = (ctor_fn)(slide + file_addr[CTOR]);
	ctor_fn dtor = (ctor_fn)(slide + file_addr[DTOR]);
	write_fn write_filter = (write_fn)(slide + file_addr[WRITE]);
	cmdptr_fn cmd_ptr = (cmdptr_fn)(slide + file_addr[CMDPTR]);
	const char *(*api_version)(void) = (const char *(*)(void))anchor;

	/* Cutoffs: from the command line, or the library's own plus a sweep. */
	int nfc = 0;
	double *fcs;
	if (argc > 2) {
		nfc = argc - 2;
		fcs = malloc(sizeof(double) * (size_t)nfc);
		for (int i = 0; i < nfc; i++)
			fcs[i] = strtod(argv[i + 2], NULL);
	} else {
		int nknown = (int)(sizeof(KNOWN_FC) / sizeof(KNOWN_FC[0]));
		fcs = malloc(sizeof(double) * (size_t)(nknown + 231));
		for (int i = 0; i < nknown; i++)
			fcs[nfc++] = KNOWN_FC[i];
		for (int k = 20; k <= 250; k++) {
			double fc = k / 1000.0;
			bool dup = false;
			for (int i = 0; i < nknown; i++)
				if ((float)fc == (float)KNOWN_FC[i])
					dup = true;
			if (!dup)
				fcs[nfc++] = fc;
		}
	}

	printf("{\"api_version\":\"%s\",\"window\":%d,\"normalise\":true,\"filters\":[\n",
	       api_version(), WIN_BLACKMAN);
	int first = 1;
	for (size_t s = 0; s < sizeof(STAGES) / sizeof(STAGES[0]); s++) {
		int n = STAGES[s].taps;
		for (int i = 0; i < nfc; i++) {
			float fc = (float)fcs[i];      /* the design function takes a float */
			double *taps = calloc((size_t)n, sizeof(double));
			design(taps, n, fc, WIN_BLACKMAN, true);

			uint8_t cl[256] __attribute__((aligned(16)));
			memset(cl, 0, sizeof(cl));
			ctor(cl);
			vec_t v = {taps, taps + n, taps + n};
			write_filter(cl, STAGES[s].stage, &v);
			const uint32_t *w = (const uint32_t *)cmd_ptr(cl, 0);
			int words = 1 + (int)((w[0] >> 16) & 0xFF);
			if (words > CMD_BYTES / 4)
				words = CMD_BYTES / 4;

			uint32_t fc_bits;
			memcpy(&fc_bits, &fc, 4);
			printf("%s{\"stage\":%d,\"taps\":%d,\"fc\":%.17g,\"fc_f32\":\"%08" PRIx32 "\",\"coeffs\":",
			       first ? "" : ",\n", STAGES[s].stage, n, (double)fc, fc_bits);
			print_hex_doubles(taps, n);
			printf(",\"command\":[");
			for (int k = 0; k < words; k++)
				printf("%s%" PRIu32, k ? "," : "", w[k]);
			printf("]}");
			first = 0;
			dtor(cl);
			free(taps);
		}
	}
	printf("\n]}\n");
	free(fcs);
	return 0;
}
