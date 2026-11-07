# yunyoo-statistics

读取云悠服务的流量和到期时间，输出 Clash 订阅响应头 `Subscription-Userinfo` 对应的字符串。

脚本自动获取登录表单的动态 token，提交登录表单，从 Cookie 中取得 JWT，再查询指定服务。仅使用 Python 标准库，无需安装第三方依赖。

## 使用方法

需要 Python 3.8 或更新版本。

```bash
git clone git@github.com:thcpdd/yunyoo-statistics.git
cd yunyoo-statistics
python3 yunyoo_clash.py --email 'you@example.com' --host-id 123456
```

将示例邮箱和服务 ID 替换为自己的值。运行后会提示输入密码，输入内容不会显示。服务 ID 是云悠服务详情链接中的 `id`，对应接口参数 `host_id`。

也可以通过环境变量配置，适合定时任务：

```bash
export YUNYOO_EMAIL='you@example.com'
export YUNYOO_HOST_ID='123456'
read -r -s -p '云悠密码: ' YUNYOO_PASSWORD
echo
export YUNYOO_PASSWORD
python3 yunyoo_clash.py
unset YUNYOO_PASSWORD
```

以上密码输入示例适用于 Bash。使用定时任务时，通过运行环境的密钥配置注入 `YUNYOO_PASSWORD`。

脚本没有预设账号、密码或服务 ID。JWT 和会话 Cookie 会保存在本地认证缓存中；密码和接口原始 JSON 不会保存，认证信息也不会打印到标准输出。

## 认证缓存与重试

首次运行时正常登录，随后缓存 JWT 和会话 Cookie。再次运行时，优先使用未过期的缓存请求服务接口，成功时不需要输入密码。

缓存请求失败时（包括 HTTP/网络错误、非 JSON、业务状态失败或订阅字段异常），脚本删除旧缓存，重新登录、更新缓存并重试一次。重新登录后的请求仍失败就报错退出，不循环重试。缓存缺失、损坏或过期时直接登录；只有实际需要登录时才读取密码。

默认缓存目录为 `~/.cache/yunyoo-statistics`；设置 `XDG_CACHE_HOME` 时使用 `$XDG_CACHE_HOME/yunyoo-statistics`。不同邮箱使用独立的哈希文件名，同一账号的多个服务共用认证缓存。缓存文件权限为 `0600`，只允许当前用户读写。缓存包含可用于登录的凭据，请勿提交到仓库或分享。

可以指定缓存目录：

```bash
python3 yunyoo_clash.py --email 'you@example.com' --host-id 123456 --cache-dir /path/to/private/cache
```

## 输出格式

成功时，标准输出只有一行，例如：

```text
upload=536870912; download=536870912; total=107374182400; expire=1893456000
```

这是响应头的值。如果需要在订阅接口中使用，将它赋给 `Subscription-Userinfo` 响应头即可。脚本本身不会启动 HTTP 服务或生成 Clash 节点配置。

| 输出字段 | 来源及计算方式 |
| --- | --- |
| `upload` | 已用总字节数的一半，向下取整 |
| `download` | 已用总字节数减去 `upload`，保留奇数余下的 1 字节 |
| `total` | `data.host_data.bwlimit` 转换为字节 |
| `expire` | `data.host_data.nextduedate`，Unix 秒级时间戳 |

接口只有总已用流量 `data.host_data.bwusage`，因此上传和下载是均分估算，二者之和等于总已用字节数。到期时间取服务到期时间，与 JWT 的有效期无关。

流量默认按 `1 GiB = 1024³` 字节换算。如果服务商使用十进制 GB，可切换为：

```bash
python3 yunyoo_clash.py --email 'you@example.com' --host-id 123456 --gb-base 1000
```

## 登录流程

1. `GET /login`，保留会话 Cookie，并解析 `loginForm` 中的隐藏 token。
2. `POST /login?action=email`，提交 token、邮箱和密码。
3. 从会话 Cookie 中读取 `ZJMF_E1F55C1511BF53FB` 对应的 JWT。
4. 携带同一会话请求 `GET /host/dedicatedserver?host_id=...`。
5. 解析订阅信息，转换为响应头字符串。

请求使用浏览器 User-Agent。脚本直接提交登录表单，不调用会把密码放进 URL 参数的可选二次验证预检查接口。作为 Python 模块使用时，可以通过 `client.login(email, password)` 的返回值或 `client.jwt` 获取 JWT。

## 参数与错误处理

| 参数 | 环境变量 | 说明 |
| --- | --- | --- |
| `--email` | `YUNYOO_EMAIL` | 必填，命令行参数优先 |
| `--host-id` | `YUNYOO_HOST_ID` | 必填，命令行参数优先 |
| `--gb-base` | — | `1024`（默认）或 `1000` |
| `--cache-dir` | `XDG_CACHE_HOME`（缓存根目录） | 指定认证缓存目录 |
| — | `YUNYOO_PASSWORD` | 未设置时交互输入密码 |

成功退出码为 `0`；登录、网络或数据解析失败时，错误信息写入标准错误，退出码为 `1`；参数错误为 `2`。单次请求超时为 60 秒，缓存请求失败后最多重新登录并重试一次。

若遇到 Cloudflare 验证、图形验证码或二次验证，仅设置 User-Agent 可能无法完成登录，脚本会报错退出。站点页面或接口字段发生变化时，需要同步调整解析逻辑。
