#!/usr/bin/env python3
# Orca Mobile 反向隧道 —— 云端中继(relay)· 多租户版
#
# 部署在公网服务器。为每个用户开一个专属公网端口给其手机;所有用户的本地
# gateway 共用一个控制端口 CTRL_PORT 建立反向隧道。relay 只搬字节,看不到
# 明文(手机↔Orca 之间是应用层 E2EE)。
#
# 用户表 users.json:
#   { "alice": {"token": "xxx", "port": 7101},
#     "bob":   {"token": "yyy", "port": 7102} }
#
# 信令与数据分离,避免裸字节流错帧:
#   CTRL <user> <token>          —— 每用户一条常驻控制连接,只走行文本(PING/PONG/NEW)
#   DATA <user> <id> <token>     —— 每个手机连接对应一条临时数据连接,头行之后纯盲转
#
# 支持 SIGHUP 热重载 users.json(新增用户即时生效,无需断开老用户)。
# 纯标准库,python3.8+。

import asyncio
import argparse
import json
import os
import signal
import sys
import time

CTRL_TAG = b"CTRL"
DATA_TAG = b"DATA"
PING_INTERVAL = 15
CTRL_READ_TIMEOUT = 45
DATA_PAIR_TIMEOUT = 30
HEADER_TIMEOUT = 10
BUF = 65536


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


class User:
    """单个租户的运行时状态。"""
    def __init__(self, name, token, port):
        self.name = name
        self.token = token.encode()
        self.port = port
        self.ctrl_writer: asyncio.StreamWriter | None = None
        self.ctrl_alive = False
        self.pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self.server: asyncio.base_events.Server | None = None  # 该用户的公网监听

    def new_id(self) -> str:
        self._next_id += 1
        return f"{self.name}-c{self._next_id}"


class Relay:
    def __init__(self, users_path: str, ctrl_port: int, host: str):
        self.users_path = users_path
        self.ctrl_port = ctrl_port
        self.host = host
        self.users: dict[str, User] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---- 用户表加载 / 热重载 ----
    def load_users_file(self) -> dict:
        with open(self.users_path, "r") as f:
            return json.load(f)

    async def sync_users(self):
        """按 users.json 增删用户的公网监听端口。已在线的控制连接不受影响。"""
        try:
            data = self.load_users_file()
        except (OSError, json.JSONDecodeError) as e:
            log("读取 users.json 失败:", e)
            return
        # 新增或更新
        for name, info in data.items():
            token, port = info["token"], int(info["port"])
            u = self.users.get(name)
            if u and u.port == port and u.token == token.encode():
                continue  # 无变化
            if u and u.server is not None:
                # 端口变了,先关旧监听
                u.server.close()
            if not u:
                u = User(name, token, port)
                self.users[name] = u
            else:
                u.token = token.encode()
                u.port = port
            # 为该用户开公网监听端口
            try:
                u.server = await asyncio.start_server(
                    self._make_phone_handler(u), self.host, port)
                log(f"用户 {name} 就绪:公网端口 :{port}")
            except OSError as e:
                log(f"用户 {name} 端口 {port} 监听失败:{e}")
        # 删除已从文件移除的用户
        for name in list(self.users):
            if name not in data:
                u = self.users.pop(name)
                if u.server is not None:
                    u.server.close()
                if u.ctrl_writer is not None:
                    try: u.ctrl_writer.close()
                    except Exception: pass
                log(f"用户 {name} 已移除")

    # ---- 握手分流(控制端口上) ----
    async def handle_ctrl_port(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), HEADER_TIMEOUT)
        except asyncio.TimeoutError:
            writer.close(); return
        parts = line.strip().split(b" ")
        if not parts:
            writer.close(); return
        tag = parts[0]
        if tag == CTRL_TAG:
            await self.handle_ctrl(parts, reader, writer, peer)
        elif tag == DATA_TAG:
            await self.handle_data(parts, reader, writer)
        else:
            writer.close()

    def _lookup(self, name_b: bytes, token_b: bytes) -> User | None:
        name = name_b.decode(errors="replace")
        u = self.users.get(name)
        if u is None or u.token != token_b:
            return None
        return u

    # ---- 控制连接:CTRL <user> <token> ----
    async def handle_ctrl(self, parts, reader, writer, peer):
        if len(parts) != 3:
            writer.close(); return
        u = self._lookup(parts[1], parts[2])
        if u is None:
            log("CTRL 鉴权失败", peer, parts[1] if len(parts) > 1 else b"")
            writer.close(); return
        if u.ctrl_writer is not None:
            log(f"用户 {u.name} 已有 CTRL,替换旧连接")
            try: u.ctrl_writer.close()
            except Exception: pass
        u.ctrl_writer = writer
        u.ctrl_alive = True
        log(f"用户 {u.name} gateway 已连接(CTRL)", peer)
        ping_task = asyncio.create_task(self._ping_loop(writer))
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), CTRL_READ_TIMEOUT)
                if not line:
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            ping_task.cancel()
            if u.ctrl_writer is writer:
                u.ctrl_writer = None
                u.ctrl_alive = False
            try: writer.close()
            except Exception: pass
            log(f"用户 {u.name} gateway 断开(CTRL)")

    async def _ping_loop(self, writer):
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                writer.write(b"PING\n")
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            pass

    # ---- 数据连接:DATA <user> <id> <token> ----
    async def handle_data(self, parts, reader, writer):
        if len(parts) != 4:
            writer.close(); return
        u = self._lookup(parts[1], parts[3])
        if u is None:
            writer.close(); return
        cid = parts[2].decode(errors="replace")
        fut = u.pending.get(cid)
        if fut is None or fut.done():
            writer.close(); return
        fut.set_result((reader, writer))

    # ---- 手机来连(在某用户的专属公网端口上) ----
    def _make_phone_handler(self, user: User):
        async def handler(reader, writer):
            await self.handle_phone(user, reader, writer)
        return handler

    async def handle_phone(self, u: User, reader, writer):
        peer = writer.get_extra_info("peername")
        if not u.ctrl_alive or u.ctrl_writer is None:
            log(f"[{u.name}] 手机来连但 gateway 不在线,拒绝", peer)
            writer.close(); return
        cid = u.new_id()
        fut = self.loop.create_future()
        u.pending[cid] = fut
        try:
            u.ctrl_writer.write(f"NEW {cid}\n".encode())
            await u.ctrl_writer.drain()
        except ConnectionError:
            u.pending.pop(cid, None); writer.close(); return
        log(f"[{u.name}] 手机连接 {cid} 来自 {peer},等待 gateway 回连…")
        try:
            g_reader, g_writer = await asyncio.wait_for(fut, DATA_PAIR_TIMEOUT)
        except asyncio.TimeoutError:
            log(f"[{u.name}] {cid} 等待 gateway 超时")
            u.pending.pop(cid, None); writer.close(); return
        finally:
            u.pending.pop(cid, None)
        log(f"[{u.name}] {cid} 隧道建立,开始盲转")
        await pipe_bidirectional(reader, writer, g_reader, g_writer)
        log(f"[{u.name}] {cid} 关闭")


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


async def main():
    ap = argparse.ArgumentParser(description="Orca Mobile 反向隧道 —— 云端中继(多租户)")
    ap.add_argument("--users", default="/opt/orca-relay/users.json", help="用户表 JSON 路径")
    ap.add_argument("--ctrl-port", type=int, default=7000, help="所有 gateway 共用的控制端口")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    args = ap.parse_args()

    relay = Relay(args.users, args.ctrl_port, args.host)
    relay.loop = asyncio.get_running_loop()
    if not os.path.exists(args.users):
        # 首次运行允许空表,靠 admin.py 写入后 SIGHUP 重载
        log(f"警告:{args.users} 不存在,先以空用户表启动")
        with open(args.users, "w") as f:
            json.dump({}, f)
    await relay.sync_users()

    # SIGHUP 热重载
    def on_hup():
        log("收到 SIGHUP,重载用户表")
        asyncio.create_task(relay.sync_users())
    relay.loop.add_signal_handler(signal.SIGHUP, on_hup)

    ctrl_srv = await asyncio.start_server(relay.handle_ctrl_port, args.host, args.ctrl_port)
    log(f"relay 启动:控制端口 :{args.ctrl_port},用户数 {len(relay.users)}")
    async with ctrl_srv:
        await ctrl_srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
