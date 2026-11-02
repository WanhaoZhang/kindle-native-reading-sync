# Kindle Native Reading Sync

保留 Kindle 原生阅读体验，在 Mac 上从保留下来的系统日志恢复**每本书、每天的阅读模型用时**，并尝试同步到微信读书。

**状态：实验性。** 已在 Kindle Paperwhite 2（固件 5.12.2.1.1）与 macOS 上验证日志解析、扫码登录，以及书城书籍和个人导入书的部分时长入账。**同一本书连续分批上传仍有“接口返回成功、统计未增加”的情况，完整批量同步尚未验证通过。** 程序会暂停并保留待核验记录，不将成功回执直接算作已到账。跨日历史补传尚未经实测。

本工具是独立社区项目，与 Amazon、腾讯及微信读书无关联，也不是官方支持的外部时长导入方案。

## 能做什么

- 不用越狱，不替换原生阅读器；通过 USB 读取日志和 `.sdr` 副文件，将分析副本保存在 Mac。
- 用 ReadingLog 解析保留日志，按书、日期和小时整理 `counted` 阅读模型增量。
- 提供本地网页、微信扫码登录、人工核对书籍版本、同步预览和确认提交。
- 使用本地 SQLite 账本记录来源、映射、已确认量和未确认提交，重复导入不会重复计时。
- 同步后回查单书累计时长、目标日期统计和阅读位置；仅完全符合预期才记为确认成功。

不能从书籍累计读数恢复已经丢失的逐日历史，不能保证阅读模型用时等于实际注意力时间，也不能承诺所有 Kindle 或微信读书账号兼容。仓库已增加 Kindle 后台组件的实验实现，默认只采集；设备兼容、性能及真实入账验收尚未完成。没有手机端应用或云服务。

## Kindle 设备端预览（开发验收中）

设备端源码在 `device/`：C 增量采集器、LIPC 事件适配器、Rust 单次 worker、KUAL 菜单和独立 Upstart 服务。常驻端不控制 Wi-Fi、不阻止休眠、不接管原生阅读器；只在明确关书且醒着、USB 未导出、空闲至少 20 秒后调度 worker。

这是 **PW2 / B0D4 / 5.12.2.1.1 专用候选实现**。已完成本台 root 环境和 ARM worker 的无网络文件/SQLite 自检；常驻服务、性能、休眠及实际入账仍在验收中。默认只采集；自动上传需要与设备及配置绑定的验收记录，不能把能运行或模拟测试通过当作完整验收通过。

- 安装、构建、停用及保留账本卸载：[设备安装说明](device/install/README.md)。
- PW2 私有分区采用 4 MiB 原始队列、4 MiB 账本、4 MiB 加 64 KiB 回滚余量，并保留 20 MiB 系统空间和 1 MiB 元数据余量；安装另计完整暂存与旧版恢复副本。空间不足时拒绝，不删除旧记录。升级恢复集保存在 `upgrade.previous`，新版本须重新验收；卸载保留账号与账本。
- 采集器恢复、容量与调度：[采集器说明](device/collector/README.md)。
- 本地服务启动后访问 `/device`，可为 Kindle 单独扫码、交接上传权及导出配置。不要把原 Mac 凭据复制成并行上传实例。
- 已有未确认记录保留在原账本；设备仅从交接后的新窗口开始。不会将以前未完成的批次装入空队列重试。
- [真机验收清单](docs/DEVICE_VALIDATION.md)涵盖性能、兼容性与真实微信读书入账；未通过前保持采集模式。源代码包含设备门禁和离线测试，不代表已实现可靠的长期自动入账。

## 安装（macOS）

需要 Python **3.11+**、Rust **1.91.0** 和 Git。先安装 [Python](https://www.python.org/downloads/macos/) 与 [Rust / rustup](https://rustup.rs/)，必要时按 macOS 提示安装命令行开发工具（`xcode-select --install`）。然后：

```sh
git clone https://github.com/WanhaoZhang/kindle-native-reading-sync.git
cd kindle-native-reading-sync
rustup toolchain install 1.91.0 --profile minimal
./setup.sh
./启动阅读同步.command
```

`setup.sh` 创建 `.venv-bridge`，安装本地二维码依赖并编译日志解析器。启动后在浏览器访问 **http://127.0.0.1:8765/**；保留终端窗口，按 Ctrl+C 停止服务。也可在 Finder 双击 `启动阅读同步.command`。**不要直接双击 `local-web/index.html`**，页面需要本地服务。

手动安装等价命令：

```sh
python3 -m venv .venv-bridge
.venv-bridge/bin/python -m pip install -r requirements.txt
cargo +1.91.0 build --locked --manifest-path readinglog-cli/Cargo.toml
.venv-bridge/bin/python local_app.py --open-browser
```

其他系统可以运行部分纯 Python 模块，但设备挂载、启动脚本和安装流程目前只支持 macOS；默认挂载点为 `/Volumes/Kindle`。未接 Kindle 时可以启动页面查看空状态。

## 每次如何使用

1. 用 Kindle **原生阅读器**正常阅读，结束后返回书库。
2. 若 Kindle 已通过 USB 连接，先在 Finder **安全弹出并拔线**。
3. 在 Kindle 首页搜索框输入英文半角 `;dm` 并执行，等待约 30 秒。这会导出现存诊断日志；不同机型或固件可能不支持。
4. 重新用 USB 连接 Mac，启动工具，点击页面的导入按钮。程序复制日志及相关副文件到 Mac 后解析。
5. 如需同步，点击登录，用微信扫码并在手机确认。
6. 为每本书搜索微信读书目标，核对**书名、作者、版本**，保存对应关系。不能只凭相似标题自动匹配。
7. 生成同步预览，核对书籍、日期和用时，再勾选并确认提交。导入、搜索和预览本身不上传阅读时长。
8. 查看确认结果，并在微信读书阅读记录中核对对应日期。返回 `needs_review` / `unknown` 时保留账本，不要反复点击、删除账本或换映射重新提交。

### 多久导出、多久上传？

**日志采集和上传是两件事。** Kindle 系统日志会轮换，保留时间随固件和使用情况变化，不能保证保存一个月。建议每次阅读结束或每天结束时执行 `;dm`，及时连接 Mac 导入留存。已经保存在 Mac 的日志可以稍后集中处理；月底才第一次导出可能已经丢失大部分记录。没有记录的日期是“未知”，不是读了零分钟。

### 微信读书没有这本书怎么办？

本工具同步**时长**，不会上传 Kindle 的电子书正文、自动购买书籍或新增书架。可以搜索书城的准确版本作为目标；若书城没有对应内容，应先自行将合法持有的文件导入微信读书，再匹配个人书架里的版本。不要把时长记入另一部同名书。个人导入书已有部分入账验证，不能保证所有格式和版本都支持。

### “确认同步”意味着什么？

一次提交仅在明确成功回执、单书累计量恰好增加该批秒数、原有阅读位置保持一致、目标日期总量恰好增加该批秒数时确认。未开始阅读的目标允许缺省位置初始化为零。统计可能延迟，程序最多进行 8 轮只读回查；**不会因此重复 POST**。

服务端返回成功但没有相应增量仍是待核验。已有待核验提交的书籍会阻止继续补写。请不要清空 `.weread/` 后“重试”，否则会丢失防重复记录。完整批量和历史跨日行为仍在研究中。

## 数据与隐私

全部运行数据存放在克隆目录内，已由 `.gitignore` 排除：

| 路径 | 内容 |
| --- | --- |
| `.weread/` | 登录凭据、本地映射、同步账本及回执；不要分享 |
| `snapshots/` | Kindle 日志、副文件与元数据复制件 |
| `logs-import/` | 累积保存的待分析日志 |
| `log-results/` | 会话、小时和按日统计 |
| `exports/` | 本地 HTML 报告、CSV 与摘要 |

网页服务仅监听 `127.0.0.1`，不将整个项目目录作为静态文件目录；写操作检查 Host、Origin 和 CSRF。授权文件权限为 `0600`。需要联系微信读书完成登录、查询和时长上报；**本地运行不等于完全离线**。本工具不上传书籍正文，但登录授权允许访问账号数据，请保管好电脑和项目目录。

迁移电脑时，原始日志、映射、账本需一起保留；来源指纹或绝对路径变化可能阻断旧预览，应先核验后重新生成预览。不要将凭据、真实日志、电子书、二维码、数据库或包含个人数据的截图发到 Issue。

## 开发与测试

```sh
python3 -m unittest discover -v
cargo +1.91.0 test --locked --manifest-path readinglog-cli/Cargo.toml
```

Python 测试仅使用生成的日志、临时目录与模拟 HTTP，不读取你的账号，不真实上传。Rust 适配器测试覆盖日志去重、匹配冲突、跨午夜和小时分配等。离线测试通过不代表微信读书真实记账行为全部可靠。

结构与开发说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可与致谢

本项目整体按 **AGPL-3.0-only** 发布；保留的第三方文件仍遵循各自原有许可。源码改编和依赖来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，全文见 [LICENSE](LICENSE)。

感谢 [ReadingLog](https://github.com/huangziwei/readinglog)、[kindle-reading-dashboard](https://github.com/zevisvei/kindle-reading-dashboard)、[MiuRead](https://github.com/miumiupy98-art/miuread-koreader)、[weread-omni](https://github.com/teng-lin/weread-omni) 和 [moon](https://github.com/AnkioTomas/moon)。
