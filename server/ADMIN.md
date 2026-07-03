# 服务器运维手册(给维护 agent 看)

> 本文件写给**维护这台穿透服务器的 AI/管理员**。目标读者是"接手运维"的人,
> 不是普通接入用户(用户看仓库根目录的 `README.md`)。
> 这里讲:服务器怎么部署的、现有用户怎么查、新用户怎么加、怎么排障、怎么回滚。

### 仓库文件布局(对照)

```
orca-mobile-gateway/
├─ README.md            用户接入向导(给用户/引导用户的 agent 看)
├─ gateway.py           用户侧:在用户电脑跑(run 建隧道 / pair 改配对码)
├─ selftest_multi.py    本机自测(改动 relay/gateway 后先跑,15 项全绿再上线)
└─ server/              服务器侧(本手册所在)
   ├─ ADMIN.md          本文件
   ├─ relay.py          中继主程序 → 部署到服务器 /opt/orca-relay/
   └─ admin.py          用户管理 → 部署到服务器 /opt/orca-relay/
```

> 服务器上 `/opt/orca-relay/` 是**平铺**的(relay.py 与 admin.py 同级),
> 与仓库里的 `server/` 子目录对应。

---

## 1. 服务概览

这台公网服务器跑一个多租户 TCP 反向隧道中继(`relay.py`),让内网用户的手机
经公网连回其电脑里的桌面 Orca。原理与协议见仓库根 `README.md` 的【背景】。

- **服务器**:Ubuntu 24.04,`python3`(仅标准库,无第三方依赖)。
- **进程**:`relay.py`,由 systemd 单元 `orca-relay` 托管(常驻、开机自启、崩溃自重启)。
- **部署目录**:`/opt/orca-relay/`
- **控制端口**:`7000`(所有用户的 gateway 共用,建隧道信令)
- **用户端口段**:`7101-7150`(每用户一个,给手机连;`admin.py` 在此段内自动分配)

### 目录内容(`/opt/orca-relay/`)

| 文件 | 说明 |
| --- | --- |
| `relay.py` | 中继主程序(多租户版) |
| `admin.py` | 用户管理工具 |
| `users.json` | 用户表 `{用户名: {token, port}}`,权限 600。**含机密,勿外传** |
| `relay.py.rollback` | 上一版 relay 备份(回滚用) |
| `users.json.rollback` | 用户表备份 |
| `relay.env` | 旧单租户版遗留的环境文件,**新版不再使用**,可忽略/删除 |

---

## 2. 连接服务器

用专用 SSH 密钥(免密)。密钥在管理员本机 `~/.ssh/orca_gateway_ed25519`:

```bash
ssh -i ~/.ssh/orca_gateway_ed25519 -o IdentitiesOnly=yes root@<PUBLIC_IP>
```

> `<PUBLIC_IP>` 为服务器公网 IP(出于安全不写进仓库,向管理员获取)。
> 若换了维护机器,需把新公钥加入服务器 `/root/.ssh/authorized_keys`(每行一把,注意换行)。

---

## 3. 日常运维命令

均在服务器 `/opt/orca-relay/` 下执行。

### 查看服务状态 / 日志

```bash
systemctl status orca-relay          # 运行状态
journalctl -u orca-relay -f          # 实时日志(手机连接、鉴权失败等都在这)
journalctl -u orca-relay -n 50       # 最近 50 行
```

### 查看现有用户

```bash
cd /opt/orca-relay && python3 admin.py list     # 列出用户名/端口/token掩码
python3 admin.py show <用户名>                   # 看某用户完整信息(含明文 token)
```

### 服务控制

```bash
systemctl restart orca-relay    # 重启(会瞬断所有连接约1-2秒,gateway/手机自动重连)
systemctl stop orca-relay       # 停服
systemctl start orca-relay      # 启服
```

> 重启只影响连接的瞬时性,**不改变 users.json/token/端口,用户无需重新配对**。

---

## 4. 新增用户

```bash
cd /opt/orca-relay && ORCA_RELAY_PUBLIC=<PUBLIC_IP> python3 admin.py add <用户名>
```

- 自动:在 `7101-7150` 段分配一个空闲端口、生成随机 token、写入 `users.json`、
  给 relay 发 `SIGHUP` **热重载(不断开其他在线用户)**。
- 输出:该用户的端口、token,以及**可直接转发给用户的接入说明**。
- `<用户名>` 是自定义隧道标签(工号/拼音等),需唯一,**与 Orca/系统账号无关**。
- `ORCA_RELAY_PUBLIC=<PUBLIC_IP>` 让输出的说明里带上正确的服务器 IP;不传则显示占位符。

把输出的 **服务器IP / 用户名 / token / 端口** 通过私密渠道发给用户,用户按根 `README.md`
的步骤接入即可。

### 端口段是否够用

段内共 50 个端口(7101-7150)。`admin.py list` 看已用数量;接近用尽时:
- 扩大端口段:改 `admin.py` 顶部 `PORT_MAX`,并在**云安全组**放行新增端口,重启服务。

---

## 5. 删除 / 查看用户

```bash
python3 admin.py remove <用户名>    # 删除并热重载(该用户立即失效)
python3 admin.py list               # 确认
python3 admin.py reload             # 手动改了 users.json 后,发 SIGHUP 生效
```

---

## 6. 云安全组(入站放行)【需在云控制台操作】

服务器本机防火墙(ufw)未启用,真正的入站管控在**云厂商安全组**。需放行入方向 TCP:

| 端口 | 用途 | 建议源 |
| --- | --- | --- |
| `7000` | gateway 控制连接 | 可收窄为用户出口 IP 段;当前为 `0.0.0.0/0` |
| `7101-7150` | 手机连接 | `0.0.0.0/0`(手机移动网络 IP 不固定) |
| `22` | SSH 运维 | 建议收窄为管理员 IP |

> 安全组当前对 7000 与端口段全开(`0.0.0.0/0`)。攻击面主要靠代码层防护兜底
> (token 鉴权 + 失败限速 + 连接上限,见根 README【内置健壮性/安全机制】)。
> 如需收紧,把 7000 的源改为公司出口网段最有效。

---

## 7. 更新 relay 代码(部署新版)

保持"不永久影响用户"的前提下部署(仅重启瞬断,自愈):

```bash
# 在管理员本机,从仓库上传新版(仓库里脚本在 server/ 下)
scp -i ~/.ssh/orca_gateway_ed25519 -o IdentitiesOnly=yes server/relay.py server/admin.py root@<PUBLIC_IP>:/opt/orca-relay/

# 在服务器:先备份再重启
ssh -i ~/.ssh/orca_gateway_ed25519 -o IdentitiesOnly=yes root@<PUBLIC_IP> '
  cd /opt/orca-relay &&
  cp relay.py relay.py.rollback &&
  systemctl restart orca-relay &&
  sleep 1 && systemctl is-active orca-relay'
```

部署后**必须验证**(见第 8 节),异常则回滚(第 9 节)。
改 relay 协议时务必保持**向后兼容**(老版 gateway 收到未知控制行应忽略而非崩溃),
并先在本机跑 `python3 selftest_multi.py` 全绿再上线。

---

## 8. 部署后验证

```bash
# a) 服务活着、用户端口都在监听
ssh ... 'systemctl is-active orca-relay; ss -lntp | grep -oE ":(7000|71[0-4][0-9])" | sort -u'

# b) 现有用户是否自动重连(看日志出现 "gateway 已连接")
ssh ... 'journalctl -u orca-relay -n 20 --no-hostname'

# c) 端到端探活(应回 HTTP/1.1 101 Switching Protocols)
printf 'GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n' | nc <PUBLIC_IP> <某在线用户端口>

# d) 鉴权反馈正常(应回 ERR unauthorized)
printf 'CTRL someone WRONGTOKEN\n' | nc <PUBLIC_IP> 7000
```

---

## 9. 回滚

部署出问题时,用备份秒回上一版:

```bash
ssh ... '
  cd /opt/orca-relay &&
  cp relay.py.rollback relay.py &&
  systemctl restart orca-relay &&
  systemctl is-active orca-relay'
```

`users.json` 若被误改,同理用 `users.json.rollback` 恢复后 `python3 admin.py reload`。

---

## 10. 常见故障(服务器侧视角)

| 现象(看 journalctl) | 含义 / 处理 |
| --- | --- |
| `CTRL 鉴权失败` 反复出现 | 有 gateway 用错 token,或被扫描。同源多次会自动拉黑。持续则核对该用户 token。 |
| `用户 X 已在线,拒绝重复连接` | 同一 token 被两处使用。确认是否 token 泄露给了第二个人。 |
| `并发连接达上限` | 某用户连接数超 64(异常/被刷)。正常用不会触发。 |
| `等待 gateway 超时` | 该用户 gateway 没在运行或掉线;让用户检查本地 gateway。 |
| 服务频繁重启(status 里 restart 计数高) | relay.py 有异常抛出。看 journalctl 栈;必要时回滚。 |

---

## 11. 关键安全红线(维护者必守)

- **`users.json` / token 绝不外泄、绝不进公开仓库**(仓库 `.gitignore` 已忽略 `users.json`)。
- 改代码提交前扫描无真实公网 IP、无真实 token(本仓库示例统一用 `203.0.113.10` / `<PUBLIC_IP>`)。
- 服务器只做字节盲转,**不要**在中间做 TLS 终止/HTTP 解析(会破坏 Orca 的端到端加密链路)。
- 重启/部署虽自愈,仍尽量选低峰期,减少对正在使用者的瞬断打扰。
