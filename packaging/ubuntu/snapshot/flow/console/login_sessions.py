"""Credential ingestion and rollback, independent of the upstream engine."""

import asyncio
import json
import math
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)
DOMAINS = {"google.com", "flow.google.com", "accounts.google.com", "www.google.com"}


def normalize_bundle(value):
    if not isinstance(value, dict) or not EMAIL.fullmatch(str(value.get("email", ""))):
        raise ValueError("登录包缺少有效的 Google 邮箱，请在已登录的 Flow 页面重新采集。")
    cookies = value.get("cookies")
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= 300:
        raise ValueError("登录包必须包含完整的 Cookie 列表（最多 300 条）。")
    result, keys = [], set()
    for item in cookies:
        if not isinstance(item, dict):
            raise ValueError("登录包格式不正确。")
        domain = str(item.get("domain", "")).lower()
        path = item.get("path", "/")
        name, data = item.get("name"), item.get("value")
        if domain.lstrip(".") not in DOMAINS:
            raise ValueError("登录包包含 Flow 登录范围之外的域名，未导入。")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]{1,256}", name)
            or not isinstance(data, str)
            or len(data) > 16384
            or any(c in data for c in "\r\n\0")
        ):
            raise ValueError("登录包中的 Cookie 名称或内容不正确。")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 2048
            or any(c in path for c in "?#\r\n")
        ):
            raise ValueError("登录包中的 Cookie 路径不正确。")
        if item.get("partitionKey"):
            # Never silently strip partition metadata and widen credential scope.
            raise ValueError("此登录包包含分区 Cookie，请使用远程登录窗口完成登录。")
        expires = item.get("expirationDate", item.get("expires"))
        if expires is not None and (
            type(expires) not in (int, float)
            or not math.isfinite(expires)
            or expires > 0
            and expires <= time.time()
        ):
            raise ValueError("登录包含有已过期或无效 Cookie，请重新采集。")
        host_only = item.get("hostOnly", not domain.startswith("."))
        key = (name, domain, path)
        if key in keys:
            raise ValueError("登录包存在重复的 Cookie，未导入。")
        keys.add(key)
        cookie = {
            "name": name,
            "value": data,
            "secure": bool(item.get("secure", False)),
            "httpOnly": bool(item.get("httpOnly", False)),
        }
        if host_only:
            if domain.startswith("."):
                raise ValueError("登录包的主机范围有冲突。")
            # CDP preserves host-only scope for a domain without a leading dot.
            cookie.update(domain=domain, path=path)
        else:
            cookie.update(domain=domain, path=path)
        same = {
            "no_restriction": "None",
            "lax": "Lax",
            "strict": "Strict",
            "None": "None",
            "Lax": "Lax",
            "Strict": "Strict",
        }.get(item.get("sameSite"))
        if same:
            cookie["sameSite"] = same
        if expires and expires > 0:
            cookie["expires"] = expires
        result.append(cookie)
    return {"email": value["email"].lower(), "cookies": result}


class Vault:
    def __init__(self, root):
        self.root = Path(root) / "private-sessions"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        key = self.root / "key"
        if not key.exists():
            try:
                with key.open("xb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
        key.chmod(0o600)
        self.cipher = Fernet(key.read_bytes())

    def put(self, value):
        ident = secrets.token_hex(24)
        path = self.root / (ident + ".enc")
        with path.open("xb") as f:
            f.write(self.cipher.encrypt(json.dumps(value).encode()))
        path.chmod(0o600)
        return ident

    def path(self, ident):
        if not re.fullmatch(r"[0-9a-f]{48}", ident):
            raise ValueError("登录包编号不正确。")
        return self.root / (ident + ".enc")

    def get(self, ident, ttl=1800):
        path = self.path(ident)
        try:
            return json.loads(self.cipher.decrypt(path.read_bytes(), ttl=ttl))
        except Exception:
            raise ValueError("登录包已过期或不可读取，请重新同步。") from None

    def remove(self, ident):
        self.path(ident).unlink(missing_ok=True)


class ImportRestored(ValueError):
    """The previous browser login was restored after an unsuccessful import."""


async def inject_verified(context, page, bundle, read_state, vault, *, finalize, check_warning=None):
    """A successful write is not authentication. Restore old cookies on failure."""
    from login_readiness import read_when_ready, resume_google_session

    old = await context.cookies()
    backup = vault.put({"cookies": old})
    restored = True
    failure = "无法写入本次登录资料"
    try:
        await context.clear_cookies()
        await context.add_cookies(bundle["cookies"])
        expected = bundle["email"]
        for attempt in range(2):
            failure = "Ubuntu 浏览器未能打开 Flow 页面"
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=45000)
            for _ in range(12):
                failure = "无法读取 Flow 页面登录状态"
                state = await read_when_ready(page, read_state)
                emails = [str(e).lower() for e in state.get("emails", [])]
                if state.get("signed_in"):
                    if emails == [expected]:
                        failure = "邮箱一致，但项目同步或窗口登记未完成"
                        return await finalize(state)
                    if emails and expected not in emails:
                        failure = "Flow 返回的邮箱与目标账号不一致"
                        break
                    failure = "Flow 未返回唯一的登录邮箱"
                else:
                    host = urlsplit(page.url).hostname
                    failure = {
                        "accounts.google.com": "Flow 跳到了 Google 登录页面，未能确认登录",
                        "flow.google.com": "Flow 页面未确认登录状态",
                    }.get(host, "Flow 跳到了其他页面，未能核对登录状态")
                await asyncio.sleep(1)
            route = urlsplit(page.url)
            if (attempt or state.get("signed_in") or route.scheme != 'https'
                    or route.hostname not in {'flow.google.com', 'accounts.google.com'}):
                break
            if check_warning is not None:
                await check_warning(page)
            recovery = await resume_google_session(context, expected, read_state, check_warning=check_warning)
            if recovery["status"] != "resumed":
                failure = recovery["reason"]
                break
        raise ValueError(failure)
    except (Exception, asyncio.CancelledError) as error:
        try:
            original = vault.get(backup)["cookies"]
            await context.clear_cookies()
            if original:
                await context.add_cookies(original)
        except Exception:
            restored = False
            raise ValueError(
                "登录导入未成功，原登录资料恢复失败。已停止此账号的任务，请使用远程登录检查。"
            ) from None
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ImportRestored(failure + "。已恢复原登录资料，可重新导入或使用远程登录。") from None
    finally:
        if restored:
            vault.remove(backup)
