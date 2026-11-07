#!/usr/bin/env python3
"""登录云悠并输出 Subscription-Userinfo 的值。仅使用 Python 标准库。

运行：python3 yunyoo_clash.py --email you@example.com --host-id YOUR_HOST_ID
密码从 YUNYOO_PASSWORD 环境变量读取，未设置时交互输入。
接口没有上下行明细，因此将总已用流量均分为上传和下载，余下的 1 字节计入下载。
流量默认按 GiB 转字节；若服务商使用十进制 GB，可传 --gb-base 1000。
"""

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE = "https://yunyoo.cc"
JWT_COOKIE = "ZJMF_E1F55C1511BF53FB"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    pass


class LoginForm(HTMLParser):
    """只解析 loginForm 内的隐藏字段，保留会话对应的动态 token。"""

    def __init__(self):
        super().__init__()
        self.inside = False
        self.action = "/login?action=email"
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "loginForm":
            self.inside = True
            self.action = attrs.get("action") or self.action
        if (self.inside and tag == "input"
                and attrs.get("type", "").lower() == "hidden"
                and attrs.get("name") and "disabled" not in attrs):
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


class YunyooClient:
    def __init__(self, timeout=60, cache_file=None):
        self.timeout = timeout
        self.cache_file = Path(cache_file) if cache_file else None
        self.cookies = http.cookiejar.LWPCookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self.jwt = None

    def load_cache(self):
        self.jwt = None
        self.cookies.clear()
        if self.cache_file is None:
            return False
        try:
            self.cookies.load(str(self.cache_file), ignore_discard=True)
        except (OSError, http.cookiejar.LoadError, ValueError):
            self.cookies.clear()
            return False
        self.jwt = next((c.value for c in self.cookies
                         if c.name == JWT_COOKIE
                         and c.domain.lstrip(".") == "yunyoo.cc"
                         and not c.is_expired()), None)
        if not self.jwt:
            self.cookies.clear()
        return self.jwt is not None

    def save_cache(self):
        if self.cache_file is None:
            return
        temporary = None
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # 临时文件默认权限 0600，原子替换避免中断时留下半份缓存。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.cache_file.parent) as stream:
                temporary = Path(stream.name)
                stream.write("#LWP-Cookies-2.0\n")
                stream.write(self.cookies.as_lwp_str(ignore_discard=True))
            os.replace(temporary, self.cache_file)
        except OSError:
            raise LoginError("无法写入认证缓存，请检查缓存目录权限") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def clear_cache(self):
        self.cookies.clear()
        self.jwt = None
        if self.cache_file is not None:
            try:
                self.cache_file.unlink(missing_ok=True)
            except OSError:
                raise LoginError("无法删除失效的认证缓存，请检查缓存文件权限") from None

    def userinfo(self, email, password_provider, host_id, gb_base=1024):
        if self.load_cache():
            try:
                result = clash_userinfo(self.subscription_info(host_id), gb_base)
            except LoginError:
                # HTTP 错误、非 JSON、业务失败及字段错误均触发一次重新登录。
                self.clear_cache()
            else:
                self.save_cache()
                return result
        # 延迟获取密码：缓存可用时，无需设置或输入密码。
        password = password_provider()
        if not password:
            raise LoginError("密码不能为空")
        self.login(email, password)
        self.save_cache()
        # 新登录后的请求只执行一次，失败直接报错，避免无限登录重试。
        return clash_userinfo(self.subscription_info(host_id), gb_base)

    def request(self, path, data=None, headers=None):
        url = urljoin(BASE, path)
        # 登录表单地址来自远端页面；凭据只提交到指定站点。
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "yunyoo.cc":
            raise LoginError("拒绝向云悠以外的地址发送请求")
        body = None if data is None else urlencode(data).encode("utf-8")
        req_headers = dict(headers or {})
        if body is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=req_headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8-sig", errors="replace")
        except HTTPError as exc:
            if exc.code in (403, 429, 503):
                raise LoginError(
                    f"HTTP {exc.code}：可能遇到 Cloudflare/风控或限流；"
                    "仅设置 User-Agent 不保证能通过，请稍后重试或使用站点支持的 API"
                ) from None
            raise LoginError(f"请求失败：HTTP {exc.code}") from None
        except (URLError, TimeoutError):
            raise LoginError("网络连接失败或超时，请检查网络后重试") from None
        if "Just a moment..." in text or "cf-chl-" in text:
            raise LoginError("遇到 Cloudflare 验证页，当前 HTTP 会话无法自动登录")
        return text

    def login(self, email, password):
        form = LoginForm()
        form.feed(self.request("/login"))
        if not form.fields.get("token"):
            raise LoginError("登录页未找到隐藏 token，可能是页面变更或返回了验证页")
        fields = dict(form.fields, email=email, password=password)
        # 直接提交实际登录表单；不调用会将密码放入 GET URL 的可选预检查。
        self.request(form.action, data=fields, headers={
            "Origin": BASE, "Referer": BASE + "/login",
        })
        matches = [c.value for c in self.cookies
                   if c.name == JWT_COOKIE
                   and c.domain.lstrip(".") == "yunyoo.cc" and not c.is_expired()]
        if not matches:
            raise LoginError(
                "登录后未收到 JWT：请检查账号密码；如需图形验证码或二次验证，"
                "需先完成该验证，脚本不会反复重试"
            )
        self.jwt = matches[-1]
        return self.jwt

    def subscription_info(self, host_id):
        text = self.request("/host/dedicatedserver?" + urlencode({"host_id": host_id}),
                            headers={"Accept": "application/json",
                                     "Referer": BASE + "/clientarea"})
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise LoginError("服务接口未返回 JSON，可能登录已失效或遇到验证页") from None
        if not isinstance(payload, dict) or str(payload.get("status")) != "200":
            raise LoginError("服务接口返回失败状态，请检查登录状态及 host_id 权限")
        try:
            host = payload["data"]["host_data"]
            if not isinstance(host, dict):
                raise TypeError
            return host
        except (KeyError, TypeError):
            raise LoginError("接口缺少 data.host_data，无法提取订阅信息") from None


def clash_userinfo(host, gb_base=1024):
    def number(key):
        try:
            value = Decimal(str(host[key]))
            if not value.is_finite() or value < 0:
                raise ValueError
            return value
        except (KeyError, ValueError, InvalidOperation):
            raise LoginError(f"订阅字段 {key} 缺失或不是有效的非负数") from None

    used = int(number("bwusage") * gb_base ** 3)
    total = int(number("bwlimit") * gb_base ** 3)
    expires = number("nextduedate")
    if expires != expires.to_integral_value() or expires <= 0:
        raise LoginError("nextduedate 必须是有效的 Unix 秒级时间戳")
    upload = used // 2
    download = used - upload
    return f"upload={upload}; download={download}; total={total}; expire={int(expires)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    email = os.environ.get("YUNYOO_EMAIL")
    host_id = os.environ.get("YUNYOO_HOST_ID")
    parser.add_argument("--email", default=email, required=not email,
                        help="登录邮箱，也可设置 YUNYOO_EMAIL")
    parser.add_argument("--host-id", type=int, default=host_id, required=not host_id,
                        help="服务 ID，也可设置 YUNYOO_HOST_ID")
    parser.add_argument("--gb-base", type=int, choices=(1000, 1024), default=1024)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                        / "yunyoo-statistics",
                        help="认证缓存目录，默认 ~/.cache/yunyoo-statistics（支持 XDG_CACHE_HOME）")
    args = parser.parse_args()
    if not args.email.strip() or args.host_id <= 0:
        parser.error("邮箱不能为空，host ID 必须为正整数")
    try:
        account = hashlib.sha256(args.email.strip().lower().encode("utf-8")).hexdigest()
        client = YunyooClient(cache_file=args.cache_dir / (account + ".cookies"))
        def password_provider():
            return os.environ.get("YUNYOO_PASSWORD") or getpass.getpass("云悠密码：", stream=sys.stderr)
        print(client.userinfo(args.email, password_provider, args.host_id, args.gb_base))
    except (LoginError, EOFError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
