# 仓库拆分与迁移

2026-11-02 起，两种工作方式分别维护：

- 网页导入版：[kindle-native-reading-sync](https://github.com/WanhaoZhang/kindle-native-reading-sync)。本地 Python 网页、USB 导入、Rust 日志解析器、人工核对上传，不需要越狱。
- Kindle 自动同步版：[kindle-device-autosync](https://github.com/WanhaoZhang/kindle-device-autosync)。C 采集器、Rust worker、KUAL/Upstart、独立 Mac 首次配置助手。目前仍需真机与入账验收，自动上传默认关闭。

## 保留已有使用记录

使用网页版无需迁移数据。更新同一工作目录中的源码，保留 `.weread/`、`snapshots/`、`logs-import/`、`log-results/` 和 `exports/`。`.weread/` 中有凭据、防重放账本和未核验回执，不应删除或复制成另一个并行上传实例。

若以后改用 Kindle 自动同步版，按新仓库配置说明让助手选择真正运行过的 Mac 项目目录。交接必须在原账本的 SQLite 写锁内创建 `device-handoff.json`；一旦该标记存在，网页版继续保留查询与日志导入能力，网页版支持的上传入口保持封锁。未核验提交留在旧账本中，不带入新设备队列重试。源码拆分本身不会交接账号，也不会上传阅读时长。

旧版 `/device` 页面已移到新仓库的独立配置助手；网页版当前不再提供设备安装/扫码交接接口。两套工具各自运行，不共用运行目录。

## 历史源码与已有安装包

拆分前最后的混合版本是 [`5423150`](https://github.com/WanhaoZhang/kindle-native-reading-sync/tree/54231504fcfda32c3c21ecc1f143119e79982ea8)。保留完整 Git 历史，不重写旧提交。此前候选安装包内固定到该提交的 `SOURCE.txt` 仍能找到相应源码，已经暂存到 Kindle 的候选包不因本次拆分而改变。

新的 Kindle 版本从新仓库构建，安装包应固定到新仓库对应提交，不把新包冒充为已经验收通过的版本。两边沿用 AGPL-3.0-only 和各第三方原许可；真实凭据、账本、日志、电子书与开发构建目录均不公开。
