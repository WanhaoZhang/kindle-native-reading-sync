# Passive device collector

`ksync-collector` is a foreground service intended for a jailbroken Kindle Linux system. It does not connect to WeRead, change Wi-Fi, draw on the framebuffer, acquire a wake lock, or install itself. Installation and the LIPC event adapter live separately in `device/install/`.

```sh
make
./ksync-collector --root /var/local/kindle-native-sync
```

The default log source is `/var/log/messages`, with rotated logs in `/var/local/log`. Production builds use Linux inotify with blocking poll, plus a thirty-second durability deadline only when state is dirty and a twenty-second deadline after a witnessed close. The collector uses `nice(19)` and requests Linux idle I/O priority when the kernel supports it. Flush/idle deadlines use the monotonic clock rather than a wall clock that NTP may change. A macOS test build must explicitly pass `--poll-ms`; this fallback is not enabled by default and is not the production scheduler.

## Interface

- `--root DIR`: private service data directory; default `/var/local/kindle-native-sync`. It must be owned by the effective user and mode `0700`.
- `--log FILE`, `--rotated-dir DIR`: input sources, useful for synthetic tests.
- `--queue-bytes N`: default 4 MiB across raw active/sealed events for the PW2 profile.
- `--reserve-bytes N`: default 24 MiB + 64 KiB free space: 20 MiB for the system, 4 MiB for the worker's bounded SQLite transaction and 64 KiB for journal overhead. The installer checks the combined component, queue, ledger and transaction budget before deployment.
- `--emit 'power awake'`: send one trusted local adapter event to the existing `ROOT/control.fifo` (mode `0600`). Does not start a collector.

Allowed adapter lines are `power awake`, `power sleeping`, `usb 0`, `usb 1`, `network 0`, and `network 1`. `reader active` / `reader idle` exist for explicit trusted reader-event adapters and tests; do not generate idle from lack of page turns. Normal operation reads OpenBook/CloseBook directly from native logs. `maintenance retry` explicitly rechecks a stopped quota; `maintenance acknowledge-gap` clears a coverage warning only after human verification. `rescan now` is an overflow/recovery test event and records a coverage warning.

Runtime state is atomically written to `ROOT/runtime.json` with `schema_version=1`, `reader_state`, `power_state`, `usb_export`, `network_connected`, `idle_since_unix`, and `bootstrap_unix`. Additional booleans `capture_blocked` and `coverage_gap` report storage or source problems. Power/network observations become unknown/disconnected on restart. Before starting a worker the collector refreshes runtime state so the worker can reject stale observations. After opening its control FIFO and writing initial runtime, every collector startup sends `refresh\n` to an existing private `adapter-control.fifo` without waiting. This asks a surviving adapter to re-observe and replay its cached power/network/USB state after collector-only respawn; it never substitutes an assumed idle state. Missing/no-reader channels are ignored, and symlinks, different owners, or group/other permissions are rejected.

Only a witnessed OpenBook after the original bootstrap begins a capture window. First startup records EOF positions rather than importing old logs; a reader already open at install needs to close and reopen. An exact native CloseBook command, or the verified PW2 lifecycle pair `ReaderBooklet stop` then `End of stop method`, marks idle; an isolated end, a title containing lifecycle words, or ReaderSDKImpl low-memory closing text does not. `ReaderBooklet start` cancels idle without waiving the first OpenBook capture requirement. A long page pause remains active. Timer lines, delayed BufferedLogger identity lines, power/ereader events, and JSONJournalSaver UTC anchors retain their original bytes and timestamps.

Worker calls are `ROOT/bin/ksync-worker --root ROOT collect` while offline, or `... sync` while connected. Both require explicit idle for at least twenty seconds, awake power, no USB export, and no storage block. Unknown states never authorize a worker. The worker remains responsible for rechecking gates, account/config binding, source evidence, and at-most-once upload semantics. OpenBook, sleep, USB export, and network loss cancel the worker conservatively.

## Durability and recovery

Raw filtered lines append to `events/active.log`. At close, sleep, USB entry, and shutdown, `fsync` precedes a durable source checkpoint and atomic rename to `events/sealed/segment-*.log`. Workers must read sealed files only. Dirty state is also fsynced/checkpointed after thirty seconds; active files are not parsed concurrently. Source checkpoint offsets exclude incomplete lines so a restart can replay them; incomplete buffers grow on demand and are freed at EOF, and drained retired-inode cursors are safely reaped, and uncommitted active-file tails are truncated on restart.

The live inode stays open across rename/unlink, so trailing bytes can be drained even when the old filename disappears. Directory rescans recover rotated plain logs. Compressed native archives are decompressed using the existing `gzip` command only outside active reading, after a one-shot size-stability delay; stable raw event identities in the parser/ledger must deduplicate overlap with live capture. This is not a promise that already-pruned source data can be recovered. Oversized lines, unresolved decode failures and overflow recovery report a coverage warning rather than inventing missing time.

Queue limits do not delete unconfirmed data. A storage block remains explicit until maintenance rechecks space. No periodic low-space retry or Wi-Fi wakeup is introduced. The sealed-directory size scan is cached; available filesystem blocks are checked fresh before each retained line. Capture checks preserve the configured 24 MiB plus 64 KiB floor, while atomic checkpoint/status writes independently preserve 20 MiB plus block-rounded temporary bytes and 64 KiB overhead. Normal source checkpoints additionally preserve the full capture/worker-scratch floor plus their own block-rounded temporary size; only the small emergency runtime marker uses the lower system-only floor. This permits a failure status below the capture threshold without consuming the system reserve. Short failed appends roll back; a failed rollback is quarantined and cannot be sealed. These are best-effort guards against concurrent firmware writers, not an exclusive filesystem reservation. Real PW2 CPU, RAM, battery, USB transition ordering, and inotify behaviour still require device acceptance tests.

## Tests

```sh
make test
```

Tests compile the actual C collector and use temporary generated logs, real subprocesses, the FIFO, and a fake worker. The portable polling option allows these tests on macOS; Linux production inotify requires a separate Linux/device run. No Kindle, credentials, or network API is used.
