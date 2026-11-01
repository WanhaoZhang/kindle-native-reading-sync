# 第三方来源和许可

Copyright (c) 2026 WanhaoZhang，项目新增代码整体以 AGPL-3.0-only 发布。此声明不更改下列第三方文件的原有许可与版权；保留源码内的作者及来源说明。

| 项目 | 固定提交 | 使用范围 | 原许可 |
| --- | --- | --- | --- |
| [huangziwei/readinglog](https://github.com/huangziwei/readinglog) | `3d5d5b394818387c42a35834e743d1b13aec6b60` | vendor 中的 workspace/native/desktop 源文件，供 Rust 解析器调用；上游文件未修改 | GPL-3.0-or-later，全文在 vendor/readinglog/LICENSE |
| [zevisvei/kindle-reading-dashboard](https://github.com/zevisvei/kindle-reading-dashboard) | `f88fa772cce1af5ac5917c0be4cae4bf6faa0ea8` | `reading-metadata/scripts/krds.py` 原样保留，作者 John Howell；不调用写入输入目录的上游 CLI | GPL-3.0，全文在 vendor/kindle-reading-dashboard/LICENSE |
| [miumiupy98-art/miuread-koreader](https://github.com/miumiupy98-art/miuread-koreader) | `ee9d6f24768349d428230036087c379dd957b30c` | `readtime_protocol.lua` 的时长 DTO、小时分桶及签名逻辑移植为 Python；不包含其 Lua UI 与运行环境 | AGPL-3.0-only；项目根 LICENSE 保留全文 |
| [AnkioTomas/moon](https://github.com/AnkioTomas/moon) | `43b2181da6eb5285b62114da85e1413d2f8464e4` | 上述签名变换的原始来源 `book.koplugin/source/wechat/app.lua`，移植见 weread_upload.py | GPL-3.0；全文在 licenses/GPL-3.0.txt |
| [teng-lin/weread-omni](https://github.com/teng-lin/weread-omni) | `88bd2e095d7d7ee423eaadf8f40653e72c5be6d4` | QR 登录、E-Ink 请求参数和协议参考；Copyright (c) 2026 Teng Lin | MIT，全文在 licenses/weread-omni-MIT.txt |

Python 移植增加了标准库传输、端点允许列表、本地凭据管理、来源证据验证和同步账本；其行为不等于对上游客户端逐字段完整复刻。上游仓库提供的是公开实现参考，不能代表微信读书官方授权、接口稳定性或完整服务端入账保证。

二维码显示依赖 qrcode / Pillow，Rust 间接依赖与版本见 Cargo.lock；各依赖遵循其包内许可。公开签名测试向量来自上游协议验证材料，不是本项目用户的账号凭据。协议中的默认 guest token 是公开客户端常量，不是个人 access token。
