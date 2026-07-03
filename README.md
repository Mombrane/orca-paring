# orca-pairing — AI 执行手册

> 本文件是写给 **AI 编码助手(如 Claude Code)** 的部署与运维说明。
> 如果你是被用户要求"照这个仓库把 Orca Mobile 内网穿透搭起来"的 AI,请从
> 【任务目标】开始,按【执行流程】逐步执行,遇到【需要人工】标记处停下来让用户操作。

---

## 任务目标

让 [Orca](https://github.com/stablyai/orca) Mobile 在 **手机与电脑不在同一网络**
(典型:电脑在公司内网,只能出方向、不能被外部连入)时,仍能连接桌面端 Orca。
做法:借助一台**公网服务器**做纯 TCP 反向隧道透传。支持**多用户**,每人一个专属端口。

## 背景知识(理解后再动手,不要跳过)

1. **为什么能纯盲转**:Orca Mobile ↔ 桌面 Orca 之间是应用层端到端加密
   (Curve25519 ECDH + XSalsa20-Poly1305),身份认证靠配对码里的 `deviceToken`,
   **与连接的主机/端口无关**。所以公网服务器只需转发字节,无法也无需解密。
   → 推论:中间**不要**做 TLS 终止 / HTTP 反代,只做 L4(TCP)透传。

2. **桌面 Orca runtime**:默认监听 `127.0.0.1:6768`。经实测某些版本是**明文 ws://**
   而非 wss://(应用层 E2EE 仍在)。盲转对两者都适用,无需关心。

3. **为什么要改写配对码**:Orca Mobile 的"自定义网络地址"输入框**只接受裸 IPv4 或
   `*.ts.net`,且端口锁死 6768**,无法填端口、无法填 `ws://`。多个用户都填同一公网 IP
   就会全部撞到同一端口,无法区分。
   → 解决:配对码本质是 `base64url(JSON{v,endpoint,deviceToken,publicKeyB64,scope})`。
   用户在桌面 Orca 正常生成配对码后,用 `gateway.py pair` **只改写其中的 endpoint**
   为 `ws://<公网IP>:<该用户专属端口>`,`deviceToken`/`publicKeyB64` 原样保留。
   改写后的码给手机扫即可。

## 架构

```
 手机 App ──:<用户端口>──► 公网服务器 ──[反向隧道]──► 用户电脑 ──► 127.0.0.1:6768 (Orca)
           每用户一个端口     relay.py                 gateway.py run
```

- 内网封入方向、放出方向 → 本地 `gateway` **主动出方向**连服务器控制端口建反向隧道。
- 手机连"用户专属公网端口" → relay 通过控制连接通知对应 gateway 回连一条数据隧道 → 盲转。

## 组件

| 文件 | 运行位置 | 作用 |
| --- | --- | --- |
| `relay.py` | 公网服务器 | 多租户中继:读 `users.json`,每用户开一个公网端口;共用控制端口 `7000` 建隧道;`SIGHUP` 热重载用户表 |
| `gateway.py` | 每个用户的电脑 | `run` 子命令建隧道并盲转;`pair` 子命令改写配对码 endpoint |
| `admin.py` | 公网服务器 | 用户管理 `add`/`list`/`show`/`remove`/`reload`;变更后自动 SIGHUP |

握手协议(信令与数据分离,避免裸流错帧):
- `CTRL <user> <token>\n` — 每用户一条常驻控制连接,只走行文本(`PING`/`PONG`/`NEW <id>`)
- `DATA <user> <id> <token>\n` — 每个手机连接对应一条临时数据连接,头行之后纯盲转

---

## 执行流程(AI 按序执行)

### 前置:向用户收集这些输入 【需要人工】

- **公网服务器**:IP、SSH 登录方式(建议先装公钥免密)。服务器需有 `python3`(标准库即可)。
- **用户列表**:每个要接入的用户起一个唯一标识(工号/拼音等,仅作隧道标签,非系统账号)。
- 记 `PUBLIC_IP=<公网IP>`、控制端口 `CTRL_PORT=7000`、用户端口段默认 `7101-7150`。

### 步骤 1 — 部署 relay 到服务器

```bash
ssh <server> 'mkdir -p /opt/orca-relay'
scp relay.py admin.py <server>:/opt/orca-relay/
ssh <server> 'test -f /opt/orca-relay/users.json || echo "{}" > /opt/orca-relay/users.json; chmod 600 /opt/orca-relay/users.json'
```

配 systemd 常驻:

```bash
ssh <server> 'cat >/etc/systemd/system/orca-relay.service <<EOF
[Unit]
Description=Orca Mobile reverse-tunnel relay (multi-tenant)
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/orca-relay/relay.py --users /opt/orca-relay/users.json --ctrl-port 7000
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now orca-relay && systemctl is-active orca-relay'
```

### 步骤 2 — 放行端口 【需要人工】

在云厂商**安全组/防火墙**放行入方向 TCP:控制端口 `7000` + 用户端口段 `7101-7150`。
(注意:很多云主机真正的入站管控在控制台安全组,不是机器上的 iptables/ufw。)
验证:`nc -z -v -w5 <PUBLIC_IP> 7000` —— `succeeded` 或 `Connection refused` 都说明包能到
(refused = 端口没监听但已放行);`timed out` = **安全组没放行**,需人工处理。

### 步骤 3 — 开通用户(服务器上,每个用户一次)

```bash
ssh <server> 'cd /opt/orca-relay && ORCA_RELAY_PUBLIC=<PUBLIC_IP> python3 admin.py add <用户名>'
```

输出会给出该用户的**端口 + token + 给用户的操作说明**,原样转发给用户。

### 步骤 4 — 用户电脑:运行 gateway 【在每个用户自己的机器上】

```bash
python3 gateway.py run --relay-host <PUBLIC_IP> --user <用户名> --token <token>
```

建议做成开机自启(macOS launchd / Linux systemd --user),否则关终端即断。

### 步骤 5 — 用户配对手机

1. 桌面 Orca → Settings → Mobile → 生成手机配对码 → **复制**(得到 `orca://pair?code=...`)。
2. 改写 endpoint 为专属端口:
   ```bash
   python3 gateway.py pair --relay-host <PUBLIC_IP> --port <该用户端口>
   ```
   (省略 `--code` 时自动读 macOS 剪贴板;也可 `--code '<配对码>'` 显式传入。)
3. 把输出的新 `orca://pair?code=...` 交给手机:Orca Mobile 扫码或粘贴。

### 步骤 6 — 验证

- 命令行探活(应返回 `HTTP/1.1 101 Switching Protocols`):
  ```bash
  printf 'GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n' | nc <PUBLIC_IP> <用户端口>
  ```
- 服务器看实时日志:`ssh <server> 'journalctl -u orca-relay -f'`,手机连上会打印
  `[<用户>] 手机连接 ... 隧道建立,开始盲转`。

---

## 运维速查

| 操作 | 命令(服务器 `/opt/orca-relay`) |
| --- | --- |
| 列出用户 | `python3 admin.py list` |
| 看某用户 token | `python3 admin.py show <用户名>` |
| 删除用户 | `python3 admin.py remove <用户名>` |
| 改了 users.json 后重载 | `python3 admin.py reload` |
| 看 relay 日志 | `journalctl -u orca-relay -f` |

## 故障排查

- **手机连不上、`nc` 测端口 timed out** → 安全组未放行该端口(步骤 2)。
- **gateway 日志停在"连不上 relay"** → 服务器 relay 未运行,或控制端口 7000 未放行,
  或本地出方向被防火墙拦。
- **relay 日志有"手机连接…等待 gateway 回连超时"** → 该用户的 gateway 没在运行
  (让用户执行步骤 4),或 gateway 连本地 6768 失败(桌面 Orca 未开 / 未启用 Mobile)。
- **relay 日志"CTRL 鉴权失败"** → gateway 的 `--user`/`--token` 与 `users.json` 不一致。
- **手机连上又秒断属正常**:Orca Mobile 会开多条连接(RPC/终端/截屏),开合频繁不代表故障。

## 安全须知(AI 必须遵守)

- **切勿**把 `users.json`、`.token`、任何真实 token / 公网 IP 提交进公开仓库
  (`.gitignore` 已忽略 `users.json`、`.token`、`.qrvenv/`)。
- 每用户 token 为随机 32 字节,只能认领自己端口的隧道。
- 服务器仅做字节转发,不解密、不接触 `deviceToken` 之外的凭证。
- 若用户把密码/密钥贴进对话,提醒其事后轮换。

## 自测(修改代码后运行)

```bash
python3 selftest.py        # 单租户闭环:握手/配对/大小数据/并发
python3 selftest_multi.py  # 多租户:隔离(A手机只到A的Orca)+ 并发 + pair 改写
```
