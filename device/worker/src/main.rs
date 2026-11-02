use anyhow::{Context, Result, bail};
use ksync_worker::*;
use serde_json::json;
use std::path::{Path, PathBuf};
fn run() -> Result<serde_json::Value> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|s| s == "--help" || s == "-h") {
        println!(
            "ksync-worker --root DIR self-test [--online-self-test]|collect|compact|sync|status|reconcile --plan-id ID|import-config --inbox DIR|export --out DIR\nDefault sync collects only. Manual trial: sync --trial --prepare --book-id KEY --max-seconds 60; then sync --trial --confirm --plan-id ID. Both commands do not waive device acceptance."
        );
        return Ok(json!({"state":"help"}));
    }
    let value = |flag: &str| -> Result<String> {
        let i = args
            .iter()
            .position(|s| s == flag)
            .context("required argument missing")?;
        args.get(i + 1).cloned().context("missing argument value")
    };
    let root =
        PathBuf::from(value("--root").unwrap_or_else(|_| "/var/local/kindle-native-sync".into()));
    let command = args
        .iter()
        .find(|s| {
            [
                "self-test",
                "compact",
                "collect",
                "sync",
                "status",
                "import-config",
                "export",
                "reconcile",
            ]
            .contains(&s.as_str())
        })
        .context("unknown command")?
        .as_str();
    if command == "self-test" {
        return Ok(self_test(
            &root,
            args.iter().any(|v| v == "--online-self-test"),
        ));
    }
    let store = Store::open(&root)?;
    match command {
        "status" => store.status(),
        "compact" => compact(&store),
        "reconcile" => {
            manual::reconcile_manual(&store, &value("--plan-id")?, &mut network::Live::default())
        }
        "import-config" => import_config(&store, Path::new(&value("--inbox")?)),
        "export" => export(&store, Path::new(&value("--out")?)),
        "collect" => parsing::collect(
            &store,
            Path::new("/mnt/us/documents"),
            Path::new("/var/local/metadata/cc.db"),
        ),
        "sync" => {
            let trial = args.iter().any(|s| s == "--trial");
            if trial {
                if args.iter().any(|s| s == "--prepare") {
                    let book = value("--book-id")?;
                    let max = value("--max-seconds")?.parse()?;
                    return manual::prepare_manual(&store, Some(&book), true, max);
                }
                if args.iter().any(|s| s == "--confirm") {
                    let id = value("--plan-id")?;
                    return manual::commit_manual(&store, &id, true, &mut network::Live::default());
                }
                bail!("trial requires explicit prepare or exact plan confirmation")
            }
            let collected = parsing::collect(
                &store,
                Path::new("/mnt/us/documents"),
                Path::new("/var/local/metadata/cc.db"),
            )?;
            if !root.join("config.json").exists() {
                return Ok(collected);
            }
            let c = Config::load(&root)?;
            if !c.validation.protocol_validation
                || !c.validation.device_validation
                || c.validation.binding_sha256 != c.binding()?
            {
                return Ok(
                    json!({"state":"collect_only","collected":collected,"network_write":false}),
                );
            }
            gate(&root, true, now())?;
            device_gate(&root, &c)?;
            let p = prepare(&store, None, false, 86400)?;
            let Some(id) = p["plan"]["plan_id"].as_str() else {
                return Ok(p);
            };
            let mut result = commit(&store, id, false, &mut network::Live::default())?;
            if result["state"] == "confirmed" {
                result["compaction"] =
                    compact(&store).unwrap_or_else(|_| json!({"state":"retained"}));
            }
            Ok(result)
        }
        _ => bail!("unknown command"),
    }
}
fn main() {
    install_signal_handlers();
    match run() {
        Ok(v) => println!("{}", serde_json::to_string_pretty(&v).unwrap()),
        Err(e) => {
            if e.downcast_ref::<rusqlite::Error>()
                .is_some_and(|v| v.sqlite_error_code() == Some(rusqlite::ErrorCode::DiskFull))
            {
                eprintln!("worker stopped: storage_blocked: SQLite ledger hard limit reached");
            } else {
                eprintln!("worker stopped: {e}");
            }
            std::process::exit(1)
        }
    }
}
