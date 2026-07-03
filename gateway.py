#!/usr/bin/env python3
# Orca Mobile 反向隧道 —— 本地网关(gateway)· 多租户版
#
# 运行在每个用户自己的 Mac(与 Orca 同机)。两个子命令:
#
#   run   —— 主动出方向连云端 relay 控制端口,建立并保活反向隧道;relay 通知
#            有手机连入时回连一条数据隧道,对接本地 Orca(127.0.0.1:6768)盲转。
#
#   pair  —— 【方案 B】把用户从桌面 Orca 复制的配对码,改写其中的 endpoint 为
#            该用户专属的 ws://<relay-host>:<user-port>,输出新配对码给手机扫。
#            deviceToken / publicKey 原样保留(认证与 E2EE 都在应用层,与端口无关)。
#
# 纯标准库,python3.8+。

import asyncio
import argparse
import base64
import json
import os
import subprocess
import sys
import time

RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]
CTRL_READ_TIMEOUT = 45
BUF = 65536


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ===================== 子命令 run =====================
class Gateway:
    def __init__(self, relay_host, ctrl_port, user, token, local_host, local_port):
        self.relay_host = relay_host
        self.ctrl_port = ctrl_port
        self.user = user
        self.token = token
        self.local_host = local_host
        self.local_port = local_port

    async def run_forever(self):
        attempt = 0
        while True:
            try:
                await self.connect_ctrl()
                attempt = 0
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                log("控制连接失败:", e)
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            log(f"{delay}s 后重连 relay…")
            await asyncio.sleep(delay)

    async def connect_ctrl(self):
        reader, writer = await asyncio.open_connection(self.relay_host, self.ctrl_port)
        writer.write(f"CTRL {self.user} {self.token}\n".encode())
        await writer.drain()
        log(f"[{self.user}] 已连上 relay 控制端口 {self.relay_host}:{self.ctrl_port}")
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), CTRL_READ_TIMEOUT)
                if not line:
                    log("relay 关闭了控制连接")
                    break
                cmd = line.strip().split(b" ")
                if cmd[0] == b"PING":
                    writer.write(b"PONG\n")
                    await writer.drain()
                elif cmd[0] == b"NEW" and len(cmd) == 2:
                    cid = cmd[1].decode(errors="replace")
                    asyncio.create_task(self.open_data(cid))
                else:
                    log("未知控制指令:", line.strip())
        finally:
            try: writer.close()
            except Exception: pass

    async def open_data(self, cid):
        try:
            l_reader, l_writer = await asyncio.open_connection(self.local_host, self.local_port)
        except OSError as e:
            log(f"{cid} 连本地 Orca {self.local_host}:{self.local_port} 失败:{e}")
            return
        try:
            r_reader, r_writer = await asyncio.open_connection(self.relay_host, self.ctrl_port)
            r_writer.write(f"DATA {self.user} {cid} {self.token}\n".encode())
            await r_writer.drain()
        except OSError as e:
            log(f"{cid} 回连 relay 数据隧道失败:{e}")
            l_writer.close()
            return
        log(f"{cid} 数据隧道建立,开始盲转")
        await pipe_bidirectional(r_reader, r_writer, l_reader, l_writer)
        log(f"{cid} 关闭")


async def pipe_bidirectional(a_reader, a_writer, b_reader, b_writer):
    async def copy(src, dst):
        try:
            while True:
                data = await src.read(BUF)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try: dst.close()
            except Exception: pass
    await asyncio.gather(copy(a_reader, b_writer), copy(b_reader, a_writer))


# ===================== 子命令 pair =====================
def decode_pairing_code(text: str) -> dict:
    """解析 orca://pair?code=... 或裸 base64url,返回 offer dict。"""
    text = text.strip()
    if text.lower().startswith("orca://"):
        if "code=" in text:
            code = text.split("code=", 1)[1].split("&")[0].split("#")[0]
        elif "#" in text:
            code = text.split("#", 1)[1]
        else:
            raise ValueError("URL 中找不到 code")
    else:
        code = text
    b64 = code.replace("-", "+").replace("_", "/")
    b64 += "=" * (-len(b64) % 4)
    return json.loads(base64.b64decode(b64))


def encode_pairing_offer(offer: dict) -> str:
    """按 Orca src/shared/pairing.ts 规则:紧凑 JSON → base64url 去 padding。"""
    raw = json.dumps(offer, separators=(",", ":")).encode()
    b64url = base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")
    return f"orca://pair?code={b64url}"


def read_clipboard() -> str | None:
    """macOS: pbpaste。其它平台返回 None。"""
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def cmd_pair(args):
    # 取输入配对码:优先 --code,否则读剪贴板
    raw = args.code
    if not raw:
        raw = read_clipboard()
        if not raw:
            print("错误:未提供 --code,且无法读取剪贴板。请在 Orca 复制配对码后重试,"
                  "或用 --code '<配对码>' 传入。", file=sys.stderr)
            sys.exit(2)
    try:
        offer = decode_pairing_code(raw)
    except Exception as e:
        print(f"错误:配对码解析失败({e})。请确认复制的是 Orca 的手机配对链接/码。",
              file=sys.stderr)
        sys.exit(2)

    for k in ("deviceToken", "publicKeyB64"):
        if k not in offer:
            print(f"错误:配对码缺少字段 {k},可能不是有效的 Orca 配对码。", file=sys.stderr)
            sys.exit(2)

    old_ep = offer.get("endpoint", "(无)")
    new_ep = f"ws://{args.relay_host}:{args.port}"
    offer["endpoint"] = new_ep
    if "v" not in offer:
        offer["v"] = 2

    new_code = encode_pairing_offer(offer)
    print("=" * 60)
    print(f"用户端口 : {args.port}")
    print(f"原 endpoint : {old_ep}")
    print(f"新 endpoint : {new_ep}")
    print("=" * 60)
    print("把下面这个配对码交给手机扫描 / 粘贴到 Orca Mobile:")
    print()
    print(new_code)
    print()
    if args.qr:
        try:
            import qrcode  # 可选依赖
            qr = qrcode.QRCode(border=1)
            qr.add_data(new_code)
            qr.print_ascii(invert=True)
        except ImportError:
            print("(未安装 qrcode 库,跳过二维码渲染;可 pip install qrcode 后加 --qr)")


# ===================== 入口 =====================
def main():
    ap = argparse.ArgumentParser(description="Orca Mobile 反向隧道 —— 本地网关(多租户)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="建立并保活反向隧道")
    r.add_argument("--relay-host", required=True)
    r.add_argument("--ctrl-port", type=int, default=7000)
    r.add_argument("--user", required=True, help="用户名(需与 relay users.json 一致)")
    r.add_argument("--token", default=os.environ.get("ORCA_TUNNEL_TOKEN", ""),
                   help="该用户 token(或环境变量 ORCA_TUNNEL_TOKEN)")
    r.add_argument("--local-host", default="127.0.0.1")
    r.add_argument("--local-port", type=int, default=6768)

    p = sub.add_parser("pair", help="改写 Orca 配对码的 endpoint 为专属端口")
    p.add_argument("--relay-host", required=True)
    p.add_argument("--port", type=int, required=True, help="该用户专属公网端口(如 7101)")
    p.add_argument("--code", default=None, help="Orca 配对码;省略则从剪贴板读取")
    p.add_argument("--qr", action="store_true", help="额外在终端渲染二维码(需 qrcode 库)")

    args = ap.parse_args()

    if args.cmd == "pair":
        cmd_pair(args)
        return

    if args.cmd == "run":
        if not args.token:
            print("错误:必须提供 --token 或环境变量 ORCA_TUNNEL_TOKEN", file=sys.stderr)
            sys.exit(2)
        gw = Gateway(args.relay_host, args.ctrl_port, args.user, args.token,
                     args.local_host, args.local_port)
        log(f"[{args.user}] gateway 启动:relay={args.relay_host}:{args.ctrl_port} "
            f"→ 本地 {args.local_host}:{args.local_port}")
        try:
            asyncio.run(gw.run_forever())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
