#!/usr/bin/env python3
# 多租户闭环自测(全在本机,无需真实服务器/手机):
#   - 两个假 Orca:alice 的 echo 前缀 "A:",bob 的前缀 "B:"(用于验证隔离)
#   - relay 多租户:读临时 users.json(alice:port 15801, bob:port 15802),ctrl 17000
#   - 两个 gateway:分别 --user alice / --user bob,各连自己的假 Orca
#   - 假手机:连 alice 端口应只到 alice 的 Orca;连 bob 端口应只到 bob 的 Orca
#   - 验证:隔离正确 + 错误 token 被拒 + 并发
#   - 顺带验证 pair 子命令改写 endpoint 正确

import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CTRL_PORT = 17000
ALICE_ORCA = 15901
BOB_ORCA = 15902
ALICE_PUB = 15801
BOB_PUB = 15802
ALICE_TOK = "tok-alice-aaa"
BOB_TOK = "tok-bob-bbb"


def make_echo(prefix: bytes):
    async def handler(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(prefix + data)  # 加前缀,证明到的是哪个 Orca
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
    return handler


async def run_proc(name, *args):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    async def drain():
        async for line in proc.stdout:
            print(f"  [{name}] {line.decode(errors='replace').rstrip()}", flush=True)
    asyncio.create_task(drain())
    return proc


async def phone_send(port: int, payload: bytes, timeout=5) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    got = b""
    try:
        while len(got) < len(payload) + 2:  # +2 for prefix
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
            if not chunk:
                break
            got += chunk
    except asyncio.TimeoutError:
        pass
    writer.close()
    return got


async def ctrl_handshake(port: int, line: str, timeout=3) -> bytes:
    """向控制端口发一行握手,返回 relay 的首行响应(用于验鉴权失败/重复账号的 ERR 反馈)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(line.encode())
    await writer.drain()
    try:
        resp = await asyncio.wait_for(reader.readline(), timeout)
    except asyncio.TimeoutError:
        resp = b""
    writer.close()
    return resp


async def main():
    results = []
    def check(label, cond):
        results.append(cond)
        print(f"  [test] {label}: {'OK' if cond else 'FAIL'}", flush=True)

    # 假 Orca
    a_orca = await asyncio.start_server(make_echo(b"A:"), "127.0.0.1", ALICE_ORCA)
    b_orca = await asyncio.start_server(make_echo(b"B:"), "127.0.0.1", BOB_ORCA)
    print("[test] 假 Orca(alice/bob)已启动", flush=True)

    # 临时 users.json
    tmpdir = tempfile.mkdtemp()
    users_path = os.path.join(tmpdir, "users.json")
    with open(users_path, "w") as f:
        json.dump({
            "alice": {"token": ALICE_TOK, "port": ALICE_PUB},
            "bob":   {"token": BOB_TOK,   "port": BOB_PUB},
        }, f)

    relay = await run_proc("relay", os.path.join(HERE, "server", "relay.py"),
                           "--users", users_path, "--ctrl-port", str(CTRL_PORT))
    await asyncio.sleep(0.6)
    gw_a = await run_proc("gw-alice", os.path.join(HERE, "gateway.py"), "run",
                          "--relay-host", "127.0.0.1", "--ctrl-port", str(CTRL_PORT),
                          "--user", "alice", "--token", ALICE_TOK,
                          "--local-port", str(ALICE_ORCA))
    gw_b = await run_proc("gw-bob", os.path.join(HERE, "gateway.py"), "run",
                          "--relay-host", "127.0.0.1", "--ctrl-port", str(CTRL_PORT),
                          "--user", "bob", "--token", BOB_TOK,
                          "--local-port", str(BOB_ORCA))
    await asyncio.sleep(1.2)

    # 1) 隔离:alice 端口 → 只到 alice 的 Orca(前缀 A:)
    got = await phone_send(ALICE_PUB, b"hello")
    check("alice端口→A: Orca", got == b"A:hello")
    # 2) 隔离:bob 端口 → 只到 bob 的 Orca(前缀 B:)
    got = await phone_send(BOB_PUB, b"world")
    check("bob端口→B: Orca", got == b"B:world")
    # 3) 大数据 + 并发(两用户交叉)
    conc = await asyncio.gather(
        phone_send(ALICE_PUB, b"a" * 1000),
        phone_send(BOB_PUB, b"b" * 1000),
        phone_send(ALICE_PUB, b"x" * 1000),
        phone_send(BOB_PUB, b"y" * 1000),
    )
    check("并发 alice#1", conc[0] == b"A:" + b"a" * 1000)
    check("并发 bob#1",   conc[1] == b"B:" + b"b" * 1000)
    check("并发 alice#2", conc[2] == b"A:" + b"x" * 1000)
    check("并发 bob#2",   conc[3] == b"B:" + b"y" * 1000)

    # 4) 错误 token 应被 relay 拒绝(gateway 用错 token 连不上 → 手机连该端口应无隧道)
    #    直接验 relay 侧:用假 DATA 帧带错 token 认领,应被关闭。这里用行为验证:
    #    临时起一个 token 错误的 gateway 顶替 alice 的控制连接?relay 会拒 → alice 仍在线。
    #    简化为:连一个未注册用户的端口不存在 → 连不上(端口未监听)。
    try:
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 15999), 1.5)
        check("未注册端口 15999 不应可连", False)
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        check("未注册端口 15999 不可连", True)

    # 5) 鉴权失败:错误 token 应收到明确的 ERR unauthorized(而非静默关闭)
    resp = await ctrl_handshake(CTRL_PORT, "CTRL alice WRONG_TOKEN\n")
    check("错token→ERR unauthorized", resp.strip() == b"ERR unauthorized")

    # 6) 未知用户名同样 unauthorized
    resp = await ctrl_handshake(CTRL_PORT, "CTRL nobody sometoken\n")
    check("未知用户→ERR unauthorized", resp.strip() == b"ERR unauthorized")

    # 7) 重复账号:alice 已由 gw_a 在线,再用正确 token 连应被拒 already_connected
    resp = await ctrl_handshake(CTRL_PORT, f"CTRL alice {ALICE_TOK}\n")
    check("重复账号→ERR already_connected", resp.strip() == b"ERR already_connected")

    # 8) 拒绝重复后,alice 原连接仍可用(隔离/在线未被破坏)
    got = await phone_send(ALICE_PUB, b"still-alive")
    check("重复被拒后 alice 仍在线", got == b"A:still-alive")

    # 9) 格式错误的 CTRL(字段数不对)→ ERR bad_request
    resp = await ctrl_handshake(CTRL_PORT, "CTRL onlyone\n")
    check("格式错→ERR bad_request", resp.strip() == b"ERR bad_request")

    # 9b) 向后兼容:模拟"老版 gateway"(只认 PING/NEW,遇到未知行会忽略而非崩溃)。
    #     用 bob 的 token 连(bob 的 gw_b 先停掉,腾出在线位),验证:
    #       - 能建立控制连接并正常收发 PING/PONG(老协议不变)
    #       - 手机连 bob 端口能正常盲转(证明老客户端行为仍工作)
    gw_b.terminate()
    await asyncio.sleep(1.5)  # 等 relay 判定 bob 旧连接断开(gw_b 停 → TCP 关闭)

    async def legacy_gateway_once():
        r, w = await asyncio.open_connection("127.0.0.1", CTRL_PORT)
        w.write(f"CTRL bob {BOB_TOK}\n".encode())
        await w.drain()
        # 老 gateway 的循环:只处理 PING/NEW,其它行(如未来的 ERR)直接忽略不崩
        while True:
            line = await asyncio.wait_for(r.readline(), 10)
            if not line:
                return
            c = line.strip().split(b" ")
            if c[0] == b"PING":
                w.write(b"PONG\n"); await w.drain()
            elif c[0] == b"NEW" and len(c) == 2:
                cid = c[1].decode()
                # 回连数据隧道 → bob 的假 Orca
                dr, dw = await asyncio.open_connection("127.0.0.1", CTRL_PORT)
                dw.write(f"DATA bob {cid} {BOB_TOK}\n".encode()); await dw.drain()
                lr, lw = await asyncio.open_connection("127.0.0.1", BOB_ORCA)
                async def cp(a, b):
                    try:
                        while True:
                            d = await a.read(65536)
                            if not d: break
                            b.write(d); await b.drain()
                    except Exception: pass
                    finally:
                        try: b.close()
                        except Exception: pass
                asyncio.create_task(asyncio.gather(cp(dr, lw), cp(lr, dw)))
            # 其它(未知/ERR)→ 忽略,继续循环(这就是老 gateway 的宽容行为)

    legacy_task = asyncio.create_task(legacy_gateway_once())
    await asyncio.sleep(0.8)  # 等老客户端建好控制连接
    got = await phone_send(BOB_PUB, b"legacy-ok")
    check("老版gateway兼容:bob端口盲转正常", got == b"B:legacy-ok")
    legacy_task.cancel()

    for p in (relay, gw_a):
        p.terminate()
    a_orca.close(); b_orca.close()
    await asyncio.sleep(0.2)

    # 10) pair 子命令改写 endpoint(纯函数级验证)
    sys.path.insert(0, HERE)
    import gateway as gwmod
    offer = {"v": 2, "endpoint": "ws://127.0.0.1:6768",
             "deviceToken": "dtok", "publicKeyB64": "pk==", "scope": "mobile"}
    code = gwmod.encode_pairing_offer(offer)
    back = gwmod.decode_pairing_code(code)
    check("pair 编解码回环", back == offer)
    # 模拟改写
    offer2 = dict(back); offer2["endpoint"] = "ws://203.0.113.10:7101"
    code2 = gwmod.encode_pairing_offer(offer2)
    back2 = gwmod.decode_pairing_code(code2)
    check("pair 改写 endpoint",
          back2["endpoint"] == "ws://203.0.113.10:7101"
          and back2["deviceToken"] == "dtok" and back2["publicKeyB64"] == "pk==")

    passed = sum(results)
    print(f"\n[test] 结果:{passed}/{len(results)} 通过", flush=True)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
