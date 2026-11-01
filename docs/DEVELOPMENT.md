# 开发说明

## 模块

- `kindle_stats.py`：只读复制副文件、导出日志与元数据，调用 KRDS 解码器；输出累计指标不能直接作为每天记录。
- `readinglog-cli`：ReadingLog Rust 库的本机适配器；阅读会话、计时、跨日切分与精确计数匹配来自上游。额外的 BufferedLogger 兼容只补身份，不增加阅读时间。
- `analyze_kindle.py` / `kindle_reports.py`：复制、调用解析器并生成报表。
- `weread_readonly.py`：显式扫码与查询，封闭端点清单，拒绝跨域认证转发。
- `weread_upload.py`：纯协议、签名与旧式单片段试验入口。新用户使用网页和同步引擎；命令行单片段入口仅用于开发，不提供真实样本。
- `sync_engine.py`：累计观测、账户隔离、书籍映射、不可变预览、提交账本、只读复核。
- `local_app.py` / `local-web/index.html`：只监听 localhost 的网页服务，不需要前端构建系统。

## 数据约束

只有精确匹配、小时分配守恒的 counted 会话可供同步。每个日期至少需要三条去重的同条 JournalSaver UTC 与本地 +08:00 时钟证据；其他时区目前阻断。小时数据包含原始来源路径与 SHA-256，准备与发送前均检查。缺失日期不补零，观察累计量回退、身份冲突、来源变化或不明确写入均阻断。

每个网络批次先持久登记 `dispatching`，然后发送一次 POST。只有单书增量、目标日期增量和阅读位置均满足要求才记为 `confirmed`。`needs_review`、`unknown`、`failed`、残留 `dispatching` 均不能自动重放。工程上的每批 600 秒限制不是官方保证或平台限额。

`SyncEngine.reconcile(batch_id)` 只对已收到明确成功回执但未确认的批次进行 GET 复核；不能用于重发。匹配后事务入账一次。若期间存在其他阅读活动导致总量不再精确匹配，应人工调查，不能直接按成功回执增加本地确认余额。

## 干净安装与本地实例

公开源码不带任何账号或原有账本，首次启动为空状态。不要把已经真实同步过的日志复制到一个空账本重新同步；新的实例没有旧实例的已发送水位。持续日常使用应在同一实例中保留数据。原先个人试验的专用 13 秒迁移逻辑未纳入公共版本。

## 依赖

ReadingLog 固定源码的 native 和 desktop workspace 文件保存在 vendor；本项目只编译本机命令行适配器，没有安装上游 Kindle 应用、更新器或桌面应用。保留 desktop 文件用于保持上游 workspace 完整。KRDS 仅取 Python 解码脚本和许可，其他上游 UI 未打包。具体提交与许可见 THIRD_PARTY_NOTICES.md。

## 可选 CI

`docs/ci-example.yml` 提供 Python（Linux/macOS）与 Rust 离线测试模板。需要时将它复制到 `.github/workflows/tests.yml` 并提交；通过 GitHub CLI 上传工作流需要额外的 workflow 授权。当前仓库仅提供模板，未启用 Actions。
