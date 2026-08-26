CC     ?= clang
CFLAGS ?= -O2 -Wall -Wextra

all: sem_shim.dylib

sem_shim.dylib: sem_shim.c
	$(CC) -dynamiclib $(CFLAGS) -o $@ $<

clean:
	rm -f sem_shim.dylib
	rm -rf sm_diag_out

.PHONY: all clean
