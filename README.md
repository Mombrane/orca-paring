# orca-pairing — 用户接入向导(给 AI 看)

> 本文件写给 **AI 编码助手(如 Claude Code)**。
> 场景:某公司已经部署好一台"Orca Mobile 内网穿透"服务器(见文末【管理员信息】),
> 现在有一个**新用户**想让自己的手机连上自己电脑里的桌面 Orca。
> 你的任务是**引导这个用户完成接入**——不需要、也不要去重新部署或修改服务器。

---

## 背景(先理解,再动手)

- 用户的电脑(通常在公司内网)只能出方向、不能被外部连入,所以手机在外网连不上它的桌面 Orca。
- 已有一台公网服务器做**纯 TCP 反向隧道透传**:用户电脑主动连服务器建隧道,手机连服务器的
  某个端口,流量经隧道回到用户电脑的 `127.0.0.1:6768`(桌面 Orca)。
- 服务器只盲转字节,看不到明文(Orca Mobile↔桌面 Orca 之间是应用层端到端加密)。
- 每个用户分配一个**专属端口**;靠改写配对码里的 endpoint 指向该端口来区分用户。

用户接入只需一个本地文件:`gateway.py`(在用户电脑跑)。`relay.py`/`admin.py` 是服务器侧的,
用户**用不到**。

## 前提:该用户已被管理员开通

用户接入前必须先从管理员处拿到 **服务器IP / 用户名 / token / 端口** 四样(详见【步骤 0】)。
这些由管理员在服务器 `admin.py add <用户名>` 分配,**不是** Orca 或系统的注册账号。
若用户还没有,先按【步骤 0】引导其联系管理员开通,不要自行去服务器操作。

---

## 接入流程(引导用户按序做)

设占位符:`PUBLIC_IP`=服务器公网IP,`USER`=用户名,`TOKEN`=token,`PORT`=专属端口。

### 步骤 0 — 先获取接入凭据(AI 主动引导,不要跳过)

这四样都由**管理员**分配,不是用户自己能生成的,也不是 Orca/系统账号:

| 凭据 | 说明 | 从哪来 |
| --- | --- | --- |
| `PUBLIC_IP` | 穿透服务器的公网 IP | 管理员 |
| `USER` | 隧道用户名(仅标签,如 `zhangsan`) | 管理员 `admin.py add` 时分配 |
| `TOKEN` | 随机字符串,该用户的凭证 | 管理员 |
| `PORT` | 该用户专属公网端口(如 `7102`) | 管理员 |

**AI 应主动这样做**:
1. 先问用户:"你是否已从管理员处拿到 服务器IP、用户名、token、端口 这四样?"
2. **已有** → 记下,进入步骤 1。
3. **没有** → 引导用户联系管理员开通:"请把你要接入的用户名(如工号/拼音)告诉管理员,
   请管理员在服务器执行 `admin.py add <用户名>`,并把生成的 服务器IP / 用户名 / token / 端口
   发给你。" 拿到后再继续。
4. **不要**让用户自己去服务器执行开通命令,也不要自己去服务器开通——除非用户本人就是管理员、
   拥有服务器登录权限。

### 步骤 1 — 确认桌面 Orca 正在运行且启用了 Mobile

桌面 Orca 需开着,且 Settings → Mobile 可用(runtime 监听 `127.0.0.1:6768`)。
可验证:`lsof -nP -iTCP:6768 -sTCP:LISTEN`(macOS)应看到 Orca 在监听。

### 步骤 2 — 运行本地网关(gateway)

在用户电脑执行(需 `python3`,仅标准库):

```bash
python3 gateway.py run --relay-host <PUBLIC_IP> --user <USER> --token <TOKEN>
```

成功日志:`[<USER>] 已连上 relay 控制端口 <PUBLIC_IP>:7000`。
> 这会占用一个前台进程。让它一直开着;需要长期可用应做成开机自启(见【可选:开机自启】)。

### 步骤 3 — 生成并改写配对码

1. 桌面 Orca → Settings → Mobile → 生成手机配对码 → 点**复制配对链接**
   (得到形如 `orca://pair?code=...` 的字符串)。
2. 改写其 endpoint 为该用户专属端口:
   ```bash
   python3 gateway.py pair --relay-host <PUBLIC_IP> --port <PORT>
   ```
   - 省略 `--code` 时自动读 macOS 剪贴板(即刚复制的那个);
   - 或显式传:`--code 'orca://pair?code=...'`。
3. 命令会打印一个**新的** `orca://pair?code=...`(endpoint 已改为 `ws://<PUBLIC_IP>:<PORT>`)。

### 步骤 4 — 手机配对

把步骤 3 输出的新配对码交给手机上的 **Orca Mobile**:扫码,或用其"粘贴配对码"入口粘进去。
若需要二维码,可用任意二维码工具把那串文本转成二维码给手机扫。

### 步骤 5 — 验证连上了

- 让用户在手机 Orca Mobile 上看是否出现其电脑的会话/终端。
- 或命令行探活(应返回 `HTTP/1.1 101 Switching Protocols`):
  ```bash
  printf 'GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n' | nc <PUBLIC_IP> <PORT>
  ```

---

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| gateway 打印 `❌ 鉴权失败` 后**停止**(不再重连) | `--user`/`--token` 与管理员下发的不一致。核对后重跑;这不是网络问题,重连无用。 |
| gateway 打印 `⚠️ 该账号已有 gateway 在线` | 同一 token 被两处同时使用(或你自己多开了)。确认没有第二个人/进程在用同一 token;若确是自己刚重启,稍等会自动重连。 |
| gateway 日志停在"连不上 relay" | `PUBLIC_IP`/端口错;或本地出方向被拦。确认能出方向访问服务器 7000。 |
| 手机连不上、`nc` 测端口 timed out | 该用户端口未在服务器安全组放行 → 联系管理员。 |
| 手机扫码后"等待…"然后失败 | gateway 没在运行(回步骤 2);或桌面 Orca 没开(步骤 1)。 |
| relay 日志"并发连接达上限" | 单个用户同时连接超过 64 条(异常/被刷)。正常用不会触发;如属正常高负载联系管理员调整上限。 |
| 连上又秒断 | 正常。Orca Mobile 会开多条连接(RPC/终端/截屏),开合频繁不代表故障。 |
| `pair` 报"配对码解析失败" | 复制的不是 Orca 的手机配对链接;重新在 Settings→Mobile 复制。 |

## 可选:开机自启(让 gateway 常驻,macOS launchd)

创建 `~/Library/LaunchAgents/dev.orca.gateway.plist`(把占位符换成实际值、路径填 gateway.py 绝对路径):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.orca.gateway</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/绝对路径/gateway.py</string>
    <string>run</string>
    <string>--relay-host</string><string>PUBLIC_IP</string>
    <string>--user</string><string>USER</string>
    <string>--token</string><string>TOKEN</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

加载:`launchctl load ~/Library/LaunchAgents/dev.orca.gateway.plist`。

## 安全须知

- token 是用户凭证,勿提交进任何公开仓库、勿在公开场合粘贴。
- 服务器只做字节转发,不解密。
- 每个用户 token 只能认领自己端口的隧道,无法冒充他人。

## 内置健壮性/安全机制(供理解,无需配置)

- **鉴权反馈**:token/用户名错误时服务器回 `ERR unauthorized`,gateway 明确报错并停止无限重连
  (不会误判为网络问题猛重试)。
- **单账号单连接**:同一 token 已在线时,后来的连接被拒(`ERR already_connected`),
  防止两人共用一个 token 互相踢占、流量串到对方电脑。
- **TCP keepalive**:所有连接开启 keepalive,手机断网等造成的半死连接会被内核回收,避免 fd 泄漏。
- **并发上限**:每用户同时最多 64 条数据连接,挡住"单端口连接洪水"打满文件描述符。
- **失败限速**:同一来源 IP 短时间多次鉴权失败会被临时拉黑,削弱扫描/刷屏。
- **协议向后兼容**:以上新增的 `ERR` 反馈不影响老版 gateway(老客户端忽略未知行)。

## 管理员信息(部署/开通用户,非普通接入用户所需)

服务器侧的 `relay.py`/`admin.py` 使用说明、systemd 部署、端口段规划等,不在本向导范围。
需要搭建或开通用户时联系服务器管理员。开通命令(管理员在服务器执行):
`cd /opt/orca-relay && python3 admin.py add <用户名>`。
