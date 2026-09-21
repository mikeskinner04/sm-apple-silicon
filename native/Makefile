CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -pthread

UNAME := $(shell uname)
ifeq ($(UNAME),Darwin)
  LIB = libsmnative.dylib
  LDFLAGS = -dynamiclib
else
  LIB = libsmnative.so
  LDFLAGS = -shared -fPIC
endif

all: $(LIB) sm_sim

$(LIB): sm_native.c
	$(CC) $(LDFLAGS) $(CFLAGS) -o $@ $<

sm_sim: sm_sim.c
	$(CC) $(CFLAGS) -o $@ $<

test: all
	python3 test_native.py

clean:
	rm -f libsmnative.so libsmnative.dylib sm_sim

.PHONY: all test clean
