# ksync-worker（设备端单次进程）

此 crate 只实现设备端解析、账本和同步 worker。没有 UI，不接管 Kindle 阅读器，不打开 Wi-Fi，不设置 RTC 唤醒或阻止休眠。默认 `sync` 为 collect-only，不能将编译通过视为真机验收通过。

## 编译与测试

```sh
cargo +1.91.0 test --locked --manifest-path device/worker/Cargo.toml
cargo +1.91.0 build --locked --release --manifest-path device/worker/Cargo.toml
```

在 Mac 上编出的程序只是本机测试版。ARM 目标是 `armv7-unknown-linux-musleabihf` 的完全静态 ELF；bundled SQLite 还需要目标一致的 musl C 编译器或受控 cross 工具链，不能混入 softfp/glibc 对象。二进制能编译不代表 PW2 旧内核、TLS、功耗已验证。

## 根目录和调用

私有 root 默认 `/var/local/kindle-native-sync`，目录 `0700`；认证、配置、数据库为 `0600`。仅读取 collector 已 fsync/封存的 `events/sealed/*.log`，不打开活动 `active.log`。

```sh
ksync-worker --root DIR self-test
ksync-worker --root DIR self-test --online-self-test
ksync-worker --root DIR import-config --inbox /mnt/us/.kindle-native-sync-inbox
ksync-worker --root DIR collect
ksync-worker --root DIR sync
ksync-worker --root DIR status
ksync-worker --root DIR export --out /mnt/us/kindle-native-sync-export
ksync-worker --root DIR compact
ksync-worker --root DIR reconcile --plan-id PLAN_ID
```

`self-test` 默认仅探测实际架构、B0D4 硬件前缀、固件、内核和私有原子文件读写，**不会联网或生成验收证书**。显式 `--online-self-test` 才向固定的 `https://i.weread.qq.com/config` 匿名 GET 一次，保留标准 CA/主机名校验，不读取或发送账号凭据。

`collect` 需要 runtime 新鲜（120 秒以内）、reader idle 20 秒、设备 awake、非 USB 导出、capture 未阻断；不要求联网。`sync` 额外要求联网且不存在待人工核验的 capture gap。所有未知状态、缺字段、未来时间均阻断。重启后的 bootstrap 与 Mac handoff cutoff 取较晚值，避免导入首次安装以前的数据。未配置时 raw journal 仍由 collector 保留，worker 返回 `collect_only_unconfigured`，不会尝试账号请求。

## 配置、认证和交接

`config.json` schema 1：`device_id`, `account_vid`, `profile`（`pw2-5.12.2.1.1`）, `firmware`（`5.12.2.1.1`）, `timezone`（`Asia/Shanghai`）, `bootstrap_cutoff_unix`, `mappings`（每项 `local_book_id/weread_book_id/title`）, `validation`。

`auth.json`：`vid/deviceId/accessToken/refreshToken`。设备需要独立的 deviceId 和授权；不得复制 Mac 现有并行使用的上传身份，也不得把旧的 455 秒待核验数据导入新账本重放。

`import-config --inbox DIR` 从 `config.json/auth.json/handoff.json` 导入，manifest 为：

```json
{"schema_version":1,"kind":"kindle-native-sync-handoff","config_sha256":"SHA256_OF_RAW_CONFIG_BYTES","auth_sha256":"SHA256_OF_RAW_AUTH_BYTES"}
```

worker 检查文件 hash、账号和设备一致，原子保存到私有 root。交接配置中的两个验收开关必须都是 false。成功后仅删除已验证的三份交接文件，若 inbox 为空才删除目录，不删除额外用户文件。导入失败不清除凭据供调查；应在 USB 用户区上及时人工清理失败交接的凭据。身份、账号或 bootstrap 不能覆盖已有账本。

## 两项验收和受控试传

`validation` 为：

```json
{"device_validation":false,"protocol_validation":false,"binding_sha256":""}
```

绑定 hash 为配置**去掉顶层 validation**后的 JSON：UTF‑8、键递归排序、无额外空格，与 Python `json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False)` 相同。`status` 输出 hash 便于人工核对，但不输出 vid 或认证令牌。

正式启用前必须先在实际 PW2 上验证 ELF、睡眠/唤醒、USB 循环和性能，将真实记录整理为 `ROOT/validation-device.json`，模板见 `examples/validation-device.example.json`。模板默认全部 false，不会自动导入或转成 true；`kernel_release` 及配置 hash 必须与现场一致。真实写入另检查实际 ARM、root、B0D4 前缀和系统固件文件，不能在 Mac 修改配置布尔就上传。

仅完成设备验收后，可对**安装后新增的真实阅读**进行人工试传：

```sh
ksync-worker --root DIR sync --trial --prepare --book-id LOCAL_KEY --max-seconds 60
# 人工核对输出中的完整会话、来源、目标书、日期和秒数，保存精确 plan_id
ksync-worker --root DIR sync --trial --confirm --plan-id PLAN_ID
```

trial 必须是一个完整的、可核验的 counted 会话，1–60 秒；不会把长会话剪成看似真实的小片段。只能在已绑定的 `device_validation=true` 时执行一次，允许 `protocol_validation=false` 以研究首次协议验收。准备阶段不联网，确认必须指向同一不可变 plan_id。

自动 `sync` 必须两项验收全部通过且 binding 相同。协议验收应覆盖同书连续会话、跨小时、历史日期、阅读位置保护和不明写入不重放，不能只凭一次 HTTP 200 或 succ=1 打开开关。合并关书会话的行为尚不能宣称已解决服务端“成功但未记账”。

## 解析和不可重放边界

复用 ReadingLog 的解析、counted 模型和小时分配；BufferedLogger 延迟内容只补精确身份，不增加秒数。副文件统计索引按文件 inode/尺寸/mtime 缓存。cc.db 以只读方式打开并缓存，仅为已精确识别的 sidecar/内容键补标题；不使用文件字节大小猜测 HTMLPosition，不改变已识别的 source key。每个日期至少三条去重后的同条 JournalSaver UTC+08 锚才允许生成待传会话。

每个 plan 在网络写入前持久 claim 为 `dispatching`；之后仅发送一个 POST，再最多两个 GET 回读。认证只在写前 GET 预检阶段续期一次，核对同 vid 并原子保存。POST 后不续期、不重发。仅在单书累计量、正确日期总量和位置均满足预期时确认。`reconcile` 仅对明确收到成功回执的 needs_review 批次做 GET 核验，不续期、不 POST。

SIGTERM/SIGINT 只设置取消标记：发送前退出，已 claim 进入发送边界则允许当前一个 POST 与两个有限 GET 收尾，再退出。单请求 HTTP budget 20 秒，此阶段最多三次请求，不阻止系统睡眠；真实断电/强制 kill 留下 dispatching 仍阻断重放。

counter 范围重叠默认阻断；OpenBook 的整秒 TimeRead 可能造成不足 1 秒的边界重叠，只有字数边界精确相邻、旧结束早于新开始且有新的明确 OpenBook 时才按上游已有分配保留为新会话。不自行重新分配小时或补造缺失时间。

## 空间、内存与 compact

SQLite `page_size=4096,max_page_count=4096`，明确限制为 16 MiB。heavy collect/commit/compact 前要求文件系统至少保留 20 MiB，加 16 MiB 本次事务/rollback 预算；不足返回 `storage_blocked`，不删除未确认数据。

一次解析的封存 raw journal 和 raw line ledger 上限为 4 MiB，超过返回 `memory_budget_blocked` 并保留源文件。SQLite 逐行分类，不构建多份完整正文；**worker 真机峰值 RSS/翻页延迟仍未实测**，4 MiB 输入限制不是内存占用保证。

`compact` 仅在最近一次解析没有任何无法关联/时区不明/冲突数据、所有 sessions 与 attempts 都 confirmed、来源 hash 均可核验时执行。事务清理 raw lines/sources，归档源 hash，保留会话 ID、counter 范围、日统计和上传回执，再仅删除这些已确认的 sealed 日志并 VACUUM。活动日志不碰；任何未确认或无法解释的片段都会阻断清理。自动 sync 成功后也会尝试该保守清理，受新一轮 runtime/取消检查约束。

无法安全 compact 时应导出、人工诊断，不能删除 ledger 重新开始。即使 compact 后原始日志的重复复制件再次出现，保留的 ID 和 counter 范围也能阻断重复上传。
