#!/usr/bin/env python3
"""Explicitly enabled WeRead E-Ink QR login and read-only queries. No uploader.

Protocol reference: teng-lin/weread-omni @
88bd2e095d7d7ee423eaadf8f40653e72c5be6d4 (MIT, Copyright 2026 Teng Lin).
See THIRD_PARTY_NOTICES.md for attribution and README.md for usage.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://i.weread.qq.com"
DEFAULT_AUTH = Path(__file__).resolve().parent / ".weread" / "auth.json"
VERSION_HEADERS = {
    "baseapi": "30", "appver": "2.1.2.10245900", "basever": "2.1.2.10245900",
    "osver": "11", "channelId": "900",
    "User-Agent": "WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)",
}
# Intentionally closed list: neither time uploads nor progress writes exist here.
ROUTES = {
    ("GET", "i.weread.qq.com", "/wxticket"),
    ("GET", "open.weixin.qq.com", "/connect/sdk/qrconnect"),
    ("GET", "long.open.weixin.qq.com", "/connect/l/qrconnect"),
    ("POST", "i.weread.qq.com", "/login"),
    *[("GET", "i.weread.qq.com", p) for p in (
        "/store/search", "/shelf/sync", "/book/getProgress", "/readdata/detail",
        "/config", "/feature", "/book/info")],
}


class BridgeError(Exception):
    """Only sanitized messages may cross this boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("服务器返回跳转，已停止；没有向新地址转发认证资料。")


class Transport:
    def __init__(self, timeout=20):
        self.timeout = timeout
        context = ssl.create_default_context()
        # Some python.org macOS installs omit their own CA file. Use the
        # system's existing CA bundle while preserving certificate validation.
        system_ca = Path('/etc/ssl/cert.pem')
        if sys.platform == 'darwin' and not context.get_ca_certs() and system_ca.is_file():
            context = ssl.create_default_context(cafile=str(system_ca))
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=context))

    def request(self, method, url, *, query=None, body=None, credentials=None):
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username or parts.password or parts.query or parts.fragment
                or (method, parts.hostname, parts.path) not in ROUTES):
            raise BridgeError("该请求不在认证及只读接口允许列表中。")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = dict(VERSION_HEADERS)
        if credentials:
            if parts.hostname != "i.weread.qq.com":
                raise BridgeError("拒绝向其他域名发送账号认证资料。")
            headers.update(vid=credentials["vid"], accessToken=credentials["accessToken"])
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            # Never include URL, headers, response body, authorization code or tokens.
            raise BridgeError(f"接口返回 HTTP {exc.code}，已停止；没有自动重试。") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            raise BridgeError("网络连接失败或超时，已停止；没有自动重试。") from None
        if len(raw) > 8 * 1024 * 1024:
            raise BridgeError("响应超过本工具的大小限制。")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise BridgeError("接口没有返回有效 JSON，已停止。") from None
        if not isinstance(value, dict):
            raise BridgeError("接口响应结构不正确。")
        for key in ("errCode", "errcode"):
            if key in value and str(value[key]) != "0":
                code = str(value[key])
                safe = code if code.lstrip("-").isdigit() else "unknown"
                raise BridgeError(f"接口报告错误码 {safe}；没有自动刷新认证或重试。")
        return value


def nonempty(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n"):
        raise BridgeError(f"响应缺少有效的 {label}。")
    return value


def load_auth(path):
    try:
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise BridgeError("认证文件必须为普通文件且仅当前用户可访问（chmod 600）。")
        value = json.loads(path.read_text(encoding="utf-8"))
        return {k: nonempty(value.get(k), k) for k in ("vid", "accessToken", "refreshToken", "deviceId")}
    except (OSError, ValueError, AttributeError):
        raise BridgeError("无法读取认证文件；请先显式运行 login。") from None


def save_auth(path, value, replace=False):
    if path.exists() and not replace:
        raise BridgeError("认证文件已存在；更换账号请显式使用 --replace-auth。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.name == ".weread":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            # Atomic no-clobber commit; cannot silently replace another login.
            os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def login_body(code, device_id, *, now_ms=None, random_value=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    random_value = secrets.randbelow(1000) if random_value is None else random_value
    return {
        "appFirstInstall": 1, "code": code, "deviceId": device_id,
        "deviceName": "BOOX", "deviceType": 3,
        "installId": "eink31" + "".join(str(secrets.randbelow(10)) for _ in range(26)),
        "isAutoLogout": 0, "isFromQrcode": 1, "random": random_value,
        "signature": hashlib.sha256(f"{now_ms}{device_id}{random_value}".encode()).hexdigest(),
        "timestamp": now_ms, "trackId": "",
    }


def login(transport, show_qr, wait_seconds=180):
    ticket = transport.request("GET", BASE + "/wxticket", query={"nonceStr": "weread"})
    signature = nonempty(ticket.get("signature"), "QR ticket signature")
    timestamp = ticket.get("timeStamp")
    if isinstance(timestamp, bool) or not str(timestamp).isdigit():
        raise BridgeError("二维码票据时间戳无效。")
    qr = transport.request("GET", "https://open.weixin.qq.com/connect/sdk/qrconnect", query={
        "appid": "wxab9b71ad2b90ff34", "noncestr": "weread", "timestamp": timestamp,
        "scope": "snsapi_userinfo,snsapi_timeline,snsapi_friend", "signature": signature,
    })
    if str(qr.get("errcode")) != "0":
        raise BridgeError("微信没有返回有效二维码。")
    uid = nonempty(qr.get("uuid"), "QR uuid")
    show_qr("https://open.weixin.qq.com/connect/confirm?" + urllib.parse.urlencode({"uuid": uid}))
    deadline, last = time.monotonic() + wait_seconds, None
    while time.monotonic() < deadline:
        params = {"f": "json", "uuid": uid}
        if last is not None:
            params["last"] = last
        value = transport.request("GET", "https://long.open.weixin.qq.com/connect/l/qrconnect", query=params)
        status = str(value.get("wx_errcode"))
        if status == "405":
            code = nonempty(value.get("wx_code"), "QR authorization code")
            break
        if status == "402":
            raise BridgeError("二维码已过期，请重新运行 login。")
        if status == "403":
            raise BridgeError("手机端取消了授权。")
        if status not in ("404", "408"):
            raise BridgeError("微信返回未知二维码状态，已停止。")
        last = status
        time.sleep(1)
    else:
        raise BridgeError("等待扫码超时，请重新运行 login。")
    device_id = "eink334691225" + str(secrets.randbits(63)).zfill(19)
    # One exchange only. Never retry a possibly consumed authorization code.
    value = transport.request("POST", BASE + "/login", body=login_body(code, device_id))
    vid = value.get("vid")
    if isinstance(vid, int) and not isinstance(vid, bool):
        vid = str(vid)
    auth = {"vid": nonempty(vid, "vid"), "deviceId": device_id}
    for key in ("accessToken", "refreshToken"):
        auth[key] = nonempty(value.get(key), key)
    return auth


def query(transport, credentials, command, args):
    if command == "shelf":
        path, params = "/shelf/sync", {}
    elif command == "search":
        path, params = "/store/search", {"keyword": args.keyword, "scope": 10, "maxIdx": 0, "count": 20}
    elif command == "getProgress":
        path, params = "/book/getProgress", {"bookId": args.book_id}
    elif command == "readdata":
        path, params = "/readdata/detail", {"mode": args.mode, "baseTime": args.base_time}
    else:
        raise BridgeError("不支持的只读命令。")
    return transport.request("GET", BASE + path, query=params, credentials=credentials)


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信读书 E-Ink 扫码与只读查询；没有上传功能。")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_AUTH)
    sub = parser.add_subparsers(dest="command")
    for name in ("login", "search", "shelf", "getProgress", "readdata"):
        child = sub.add_parser(name)
        child.add_argument("--online", action="store_true", help="显式允许本次命令联网")
        if name == "login":
            child.add_argument("--wait-seconds", type=int, default=180, choices=range(1, 601), metavar="1..600")
            child.add_argument("--replace-auth", action="store_true")
            child.add_argument("--qr-png", type=Path, help="可选本地二维码图片；需要 qrcode[pil]")
        elif name == "search":
            child.add_argument("keyword")
        elif name == "getProgress":
            child.add_argument("book_id")
        elif name == "readdata":
            child.add_argument("--mode", choices=("weekly", "monthly", "annually", "overall"), default="monthly")
            child.add_argument("--base-time", type=int, default=0, help="周期内的 Unix 秒，0为当前周期")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if not args.online:
        parser.error("默认不联网；明确需要连接时为该命令添加 --online。")
    try:
        if args.command == "login":
            if args.credentials.exists() and not args.replace_auth:
                raise BridgeError("认证文件已存在，请先使用只读查询或显式 --replace-auth。")
            qr_module = None
            if args.qr_png:
                try:
                    import qrcode as qr_module
                except ImportError:
                    raise BridgeError("尚未安装可选 qrcode[pil]；本次尚未请求二维码。") from None
            def show_qr(url):
                if qr_module:
                    args.qr_png.parent.mkdir(parents=True, exist_ok=True)
                    qr_module.make(url).save(args.qr_png)
                    print("本地二维码：", args.qr_png, flush=True)
                else:
                    print("请将此临时授权地址在本地生成二维码后用微信扫描：", url, flush=True)
            auth = login(Transport(), show_qr, args.wait_seconds)
            # Read-only verification before persisting tokens; no account switch is hidden.
            shelf = Transport().request("GET", BASE + "/shelf/sync", credentials=auth)
            if not isinstance(shelf.get("books"), list):
                raise BridgeError("只读书架验证失败，未保存认证资料。")
            save_auth(args.credentials, auth, args.replace_auth)
            print("登录和只读书架验证完成；认证资料仅保存于本机。")
        else:
            value = query(Transport(), load_auth(args.credentials), args.command, args)
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except BridgeError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("本地文件操作失败；没有打印认证资料或自动重试登录。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
