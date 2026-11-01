CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror
LDFLAGS ?=
.PHONY: all test clean
all: ksync-collector
ksync-collector: ksync-collector.c
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)
test: ksync-collector
	python3 test_collector.py
clean:
	rm -f ksync-collector
