# PW2 device installation (experimental, root required)

These scripts do not jailbreak a device. They require a separately verified jailbreak/root environment on **B0D4 PW2 / firmware 5.12.2.1.1**, and reject other firmware or machines. Nothing here automatically deploys from a Mac. Do not upgrade firmware to make an unrelated package fit.

1. Build using `device/build/build.sh` on a development computer with NiLuJe `kindlepw2` C toolchain, Rust 1.91.0 and an ARM musl C compiler for bundled SQLite. Missing cross tools cause a hard error; Mac/Linux host binaries are not relabeled as Kindle binaries. ELF inspection checks ARM32, static linkage and soft-float C ABI. Generated packages have checksums but **are not signed releases and are not device-validated**.
2. After reviewing and verifying the package, copy the candidate directory to `/mnt/us/kindle-native-sync-package` (USB root `kindle-native-sync-package/`) and copy its `kual/` contents to `/mnt/us/extensions/kindle-native-sync/`. KUAL provides **Export root compatibility probe** and **Install / upgrade (collection only)** entries for first installation; no SSH is required if KUAL is already working. Safely eject USB, then run `sh PATH/install/probe.sh` from an existing root shell / trusted script launcher. The probe does not write; it prints a model prefix of only four bytes, never a full serial.
3. Review probe results, battery/space and recovery arrangements. `sh PATH/install/install.sh PATH` requires at least **64 MiB free in `/var/local`**. The install briefly remounts root writable only for two independent Upstart jobs, then restores it read-only. It never stops Amazon's reader/framework/power/USB daemons.
4. Binaries, credentials and ledger live in `/var/local/kindle-native-sync`, never on a FAT partition. Upstart runs collector and event adapter at low priority. Optional KUAL files are in `kual/`; copying these to `/mnt/us/extensions/kindle-native-sync/` exposes start, stop, status, import, safe export and uninstall actions.
5. Default mode is **collection only**. The worker requires independent protocol and device validation before automatic uploads. The installer never manufactures those validations. Root self-test command: `/var/local/kindle-native-sync/bin/ksync-worker --root /var/local/kindle-native-sync self-test`.

## Handoff and credentials

The Mac bridge freezes Mac writes before handing over, requires a separate QR login/deviceId and keeps any prior unknown uploads isolated. Device collection starts strictly after the immutable cutoff; do not move old snapshots to a new empty ledger. The `.kindle-native-sync-inbox` USB directory is **temporary, unprotected credential staging**; FAT cannot enforce Unix file permissions. After safe eject, choose Import USB configuration. Only the worker may validate the hashes, atomically install auth under private root permissions, then remove USB `auth.json`. If import fails, keep Mac fenced and remove the staging auth manually when no longer needed; do not publish any inbox files.

## Event adapter and power

The C adapter blocks on wildcard LIPC events from powerd, wifid, volumd and hal. Startup reads authoritative state, establishes subscriptions, then reads again. Only complete, recognized power/connectivity/USB state-event lines are processed; signal-strength, scan, battery and other unrelated events are ignored. Unsafe transitions apply immediately; safe transitions read only the corresponding properties. Repeated same-state events do not fork getters. There is no timer loop, RTC wakeup, Wi-Fi enabling or sleep inhibition. It emits conservative states if queries fail, and stops if a listener fails. Four small firmware LIPC listener processes are part of its resource footprint; measure the whole process tree on PW2. Reader active/idle comes from the collector's native log parsing, not a guessed idle timer.

This still requires real-device tests: event subscriptions, USB transitions, full sleep/resume, reboot, log rotation, throughput and battery drain. Software cannot upload while the CPU is suspended. Upload is opportunistic on natural awake/idle/connected periods.

The adapter also owns private `adapter-control.fifo` (0600, root-owned, symlinks rejected). Every collector startup requests `refresh\n` after it opens its own control FIFO and publishes runtime state. This forces a full authoritative snapshot and replays unchanged power/network/USB state after collector respawn; it does not rely on Upstart restarting the adapter and does not add timer polling. The root probe also checks the firmware `gzip` utility, used only for idle catch-up of rotated native logs.

## Upgrade, rollback and removal

Re-running install stages complete binaries and preserves the previous set as `bin.previous`; a further upgrade refuses to overwrite this rollback copy. Credentials/ledger are never reset during upgrades. Uninstall stops only this project's services, removes its jobs and active binaries, and **keeps private ledger/auth/config**. Inspect and separately erase private data only when no upload is unresolved. Do not delete a ledger merely to retry a cloud request.

Collector queue limits remain 16 MiB with a 20 MiB filesystem reserve. A full queue must stop collection safely rather than delete unsent data. Root probe output and worker status can be explicitly exported to `.kindle-native-sync-export/`; neither raw logs, auth nor ledger is exported by the control script.

## Experimental compiler fallback

`KSYNC_C_PROFILE=experimental-static-musl` permits an explicit soft-float ARM musl compiler wrapper through `KSYNC_CC`; the default remains NiLuJe kindlepw2. This fallback is labeled in the package manifest and is only a static ARM candidate, not a compatibility claim. `KSYNC_MUSL_CC` independently supplies the hard-float ARM musl compiler for SQLite in the Rust worker; optional `KSYNC_MUSL_AR` supplies a matching archiver. Do not mix hard-float SQLite objects into soft-float dynamically linked C helpers.

For the reproducible Zig 0.13.0 candidate path, obtain the official release archive separately and verify its published checksum. The ARM macOS `zig-macos-aarch64-0.13.0.tar.xz` archive SHA-256 is `46fae219656545dfaf4dce12fb4e8685cec5b51d721beee9389ab4194d43394c` (archive checksum, not the extracted executable). Then set `KSYNC_ZIG` to the extracted executable and run:

```sh
KSYNC_ZIG=/absolute/path/to/verified/zig ./device/build/build.sh --experimental-static-musl
```

The checked-in wrappers choose distinct soft-ABI collector/adapter and hard-ABI musl SQLite targets; the hard-ABI wrapper removes only cc-rs's known incompatible `--target=arm-unknown-linux-musleabihf` argument. No wrapper downloads tools, executes on Kindle, or enables validation flags. The build requires the executable's reported version to be exactly 0.13.0. For other host architectures, use the corresponding official archive and its own published checksum.
