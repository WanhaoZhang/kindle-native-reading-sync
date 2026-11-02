//! Manual commands request a fresh authoritative adapter snapshot only when
//! runtime has actually expired. Never touch or synthesize runtime.json.
use crate::*;
use anyhow::{Context, Result, ensure};
use std::{
    fs,
    io::Write,
    os::unix::{
        fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
        io::AsRawFd,
    },
    path::Path,
    time::{Duration, Instant},
};

fn private_fifo(md: &fs::Metadata, owner: libc::uid_t) -> bool {
    md.file_type().is_fifo() && md.uid() == owner && (md.mode() & 0o777) == 0o600
}
fn refresh(root: &Path) -> Result<()> {
    check_cancel()?;
    let dir = fs::symlink_metadata(root)?;
    ensure!(
        dir.is_dir()
            && !dir.file_type().is_symlink()
            && dir.uid() == unsafe { libc::geteuid() }
            && (dir.mode() & 0o077) == 0,
        "unsafe private root for adapter refresh"
    );
    let path = root.join("adapter-control.fifo");
    let before = fs::symlink_metadata(&path)?;
    ensure!(
        private_fifo(&before, unsafe { libc::geteuid() }),
        "unsafe adapter control FIFO"
    );
    let mut fd = fs::OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&path)
        .context("adapter refresh unavailable (no live reader)")?;
    let opened = fd.metadata()?;
    ensure!(
        private_fifo(&opened, unsafe { libc::geteuid() })
            && opened.dev() == before.dev()
            && opened.ino() == before.ino(),
        "adapter control FIFO changed during open"
    );
    // Keep the check tied to the open descriptor; no pathname is re-opened.
    let mut actual = std::mem::MaybeUninit::<libc::stat>::uninit();
    ensure!(
        unsafe { libc::fstat(fd.as_raw_fd(), actual.as_mut_ptr()) } == 0,
        "adapter descriptor cannot be checked"
    );
    check_cancel()?;
    fd.write_all(b"refresh\n")
        .context("adapter refresh could not be delivered")?;
    Ok(())
}

pub fn gate_manual(root: &Path, network: bool) -> Result<Runtime> {
    let path = root.join("runtime.json");
    let meta = fs::symlink_metadata(&path)?;
    // A missing, future-dated, symlink, or malformed fresh runtime is not an
    // excuse to request/assume a safe state; the ordinary gate must reject it.
    ensure!(
        meta.is_file() && !meta.file_type().is_symlink(),
        "unsafe runtime file"
    );
    let previous = meta.modified()?;
    let age = std::time::SystemTime::now().duration_since(previous);
    if !age.is_ok_and(|v| v > Duration::from_secs(120)) {
        return gate(root, network, now());
    }
    refresh(root)?;
    let deadline = Instant::now() + Duration::from_secs(6);
    let mut unsafe_since = None;
    loop {
        check_cancel()?;
        if let Ok(observed) = fs::symlink_metadata(&path) {
            ensure!(
                observed.is_file() && !observed.file_type().is_symlink(),
                "runtime changed to an unsafe file"
            );
            if observed.modified().is_ok_and(|v| v > previous) {
                // forced_snapshot deliberately publishes negative transitions
                // first. Give the actual observations up to two seconds to
                // finish; no flag is manufactured or changed by this worker.
                let raw: Runtime = serde_json::from_slice(&limited(&path, 16384)?)?;
                ensure!(
                    raw.reader_state == "idle",
                    "manual command blocked: reader is active or unknown"
                );
                if let Ok(runtime) = gate(root, network, now()) {
                    return Ok(runtime);
                }
                let started = unsafe_since.get_or_insert_with(Instant::now);
                if started.elapsed() >= Duration::from_secs(2) {
                    return gate(root, network, now());
                }
            }
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    anyhow::bail!("authoritative runtime refresh did not become safe before deadline")
}

pub fn prepare_manual(
    store: &Store,
    book: Option<&str>,
    trial: bool,
    max_seconds: i64,
) -> Result<Value> {
    gate_manual(&store.root, false)?;
    prepare(store, book, trial, max_seconds)
}
pub fn commit_manual<T: crate::network::Transport>(
    store: &Store,
    id: &str,
    trial: bool,
    t: &mut T,
) -> Result<Value> {
    gate_manual(&store.root, true)?;
    commit(store, id, trial, t)
}
pub fn reconcile_manual<T: crate::network::Transport>(
    store: &Store,
    id: &str,
    t: &mut T,
) -> Result<Value> {
    gate_manual(&store.root, true)?;
    reconcile(store, id, t)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{ffi::CString, io::Read, os::unix::fs::PermissionsExt, sync::mpsc};
    use tempfile::TempDir;
    fn setup(stale: bool, reader: &str) -> TempDir {
        let t = TempDir::new().unwrap();
        fs::set_permissions(t.path(), fs::Permissions::from_mode(0o700)).unwrap();
        atomic_json(&t.path().join("runtime.json"),&json!({"schema_version":1,"reader_state":reader,"power_state":"awake","usb_export":false,"network_connected":true,"idle_since_unix":now()-60,"bootstrap_unix":1000})).unwrap();
        if stale {
            let f = fs::OpenOptions::new()
                .write(true)
                .open(t.path().join("runtime.json"))
                .unwrap();
            f.set_times(
                fs::FileTimes::new()
                    .set_modified(std::time::SystemTime::now() - Duration::from_secs(180)),
            )
            .unwrap();
        }
        t
    }
    fn fifo(root: &Path) {
        let p = CString::new(
            root.join("adapter-control.fifo")
                .as_os_str()
                .as_encoded_bytes(),
        )
        .unwrap();
        assert_eq!(unsafe { libc::mkfifo(p.as_ptr(), 0o600) }, 0);
        fs::set_permissions(
            root.join("adapter-control.fifo"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    }
    fn fake_adapter(root: &Path, temporary_unsafe: bool) -> std::thread::JoinHandle<()> {
        let root = root.to_owned();
        let (tx, rx) = mpsc::channel();
        let handle = std::thread::spawn(move || {
            let mut pipe = fs::OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
                .open(root.join("adapter-control.fifo"))
                .unwrap();
            tx.send(()).unwrap();
            let deadline = Instant::now() + Duration::from_secs(3);
            let mut raw = Vec::new();
            let mut buffer = [0u8; 32];
            while Instant::now() < deadline && !raw.contains(&b'\n') {
                match pipe.read(&mut buffer) {
                    Ok(n) => raw.extend_from_slice(&buffer[..n]),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(e) => panic!("fake FIFO read: {e}"),
                };
                std::thread::sleep(Duration::from_millis(5));
            }
            assert_eq!(raw, b"refresh\n");
            if temporary_unsafe {
                let mut v: Value =
                    serde_json::from_slice(&fs::read(root.join("runtime.json")).unwrap()).unwrap();
                v["usb_export"] = json!(true);
                v["power_state"] = json!("sleeping");
                v["network_connected"] = json!(false);
                atomic_json(&root.join("runtime.json"), &v).unwrap();
                std::thread::sleep(Duration::from_millis(100));
            }
            let mut v: Value =
                serde_json::from_slice(&fs::read(root.join("runtime.json")).unwrap()).unwrap();
            v["usb_export"] = json!(false);
            v["power_state"] = json!("awake");
            v["network_connected"] = json!(true);
            atomic_json(&root.join("runtime.json"), &v).unwrap();
        });
        rx.recv_timeout(Duration::from_secs(2)).unwrap();
        handle
    }
    #[test]
    fn stale_runtime_refreshes_through_real_fifo_and_actual_new_file() {
        let t = setup(true, "idle");
        fifo(t.path());
        let h = fake_adapter(t.path(), false);
        assert!(gate_manual(t.path(), true).is_ok());
        h.join().unwrap();
    }
    #[test]
    fn temporary_negative_snapshot_waits_for_real_safe_observations() {
        let t = setup(true, "idle");
        fifo(t.path());
        let h = fake_adapter(t.path(), true);
        assert!(gate_manual(t.path(), true).is_ok());
        h.join().unwrap();
    }
    #[test]
    fn no_fifo_reader_fails_without_blocking() {
        let t = setup(true, "idle");
        fifo(t.path());
        let start = Instant::now();
        assert!(gate_manual(t.path(), true).is_err());
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn symlink_and_loose_permissions_are_rejected() {
        let t = setup(true, "idle");
        fifo(t.path());
        let p = t.path().join("adapter-control.fifo");
        fs::set_permissions(&p, fs::Permissions::from_mode(0o622)).unwrap();
        assert!(
            gate_manual(t.path(), true)
                .unwrap_err()
                .to_string()
                .contains("unsafe adapter")
        );
        fs::rename(&p, t.path().join("real.fifo")).unwrap();
        std::os::unix::fs::symlink(t.path().join("real.fifo"), &p).unwrap();
        assert!(
            gate_manual(t.path(), true)
                .unwrap_err()
                .to_string()
                .contains("unsafe adapter")
        );
    }
    #[test]
    fn fresh_active_reader_never_uses_refresh_to_assume_idle() {
        let t = setup(false, "active");
        assert!(gate_manual(t.path(), true).is_err());
        assert!(!t.path().join("adapter-control.fifo").exists());
    }
    #[test]
    fn refreshed_active_reader_is_still_blocked() {
        let t = setup(true, "active");
        fifo(t.path());
        let h = fake_adapter(t.path(), false);
        assert!(gate_manual(t.path(), true).is_err());
        h.join().unwrap();
    }
    #[test]
    fn fifo_owner_and_type_checks_are_strict() {
        let t = setup(true, "idle");
        fifo(t.path());
        let md = fs::metadata(t.path().join("adapter-control.fifo")).unwrap();
        assert!(!private_fifo(&md, unsafe { libc::geteuid() } + 1));
        let regular = fs::metadata(t.path().join("runtime.json")).unwrap();
        assert!(!private_fifo(&regular, unsafe { libc::geteuid() }));
    }
}
