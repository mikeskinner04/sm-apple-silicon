CC     ?= clang
CFLAGS ?= -O2 -Wall -Wextra

all: sem_shim.dylib native

sem_shim.dylib: sem_shim.c
	$(CC) -dynamiclib $(CFLAGS) -o $@ $<

native:
	$(MAKE) -C native

test: native
	$(MAKE) -C native test

clean:
	rm -f sem_shim.dylib
	rm -rf sm_diag_out
	$(MAKE) -C native clean

.PHONY: all native test clean
