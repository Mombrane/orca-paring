#!/usr/bin/env python3
# 本地闭环自测:在同一台机器上跑
#   - 假 Orca(echo 服务器,监听 127.0.0.1:16768)
#   - relay(public=15768 给"手机", ctrl=17000 给 gateway)
#   - gateway(连 relay ctrl=17000,回连本地假 Orca=16768)
#   - 假手机:连 relay 的 public=15768,发数据,校验 echo 回来的一致
# 全程无需真实服务器/手机,验证握手、配对、双向盲转、并发。

import asyncio
import os
import sys

TOKEN = "test-token-123"
ORCA_PORT = 16768
PUBLIC_PORT = 15768
CTRL_PORT = 17000
HERE = os.path.dirname(os.path.abspath(__file__))


async def fake_orca(reader, writer):
    # echo:把收到的字节原样写回(模拟 Orca 的 TLS 字节流被盲转)
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def start_fake_orca():
    srv = await asyncio.start_server(fake_orca, "127.0.0.1", ORCA_PORT)
    return srv


async def run_proc(name, *args):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    async def drain():
        assert proc.stdout
        async for line in proc.stdout:
            print(f"  [{name}] {line.decode(errors='replace').rstrip()}", flush=True)
    asyncio.create_task(drain())
    return proc


async def phone_roundtrip(label, payload: bytes) -> bool:
    reader, writer = await asyncio.open_connection("127.0.0.1", PUBLIC_PORT)
    writer.write(payload)
    await writer.drain()
    got = b""
    while len(got) < len(payload):
        chunk = await asyncio.wait_for(reader.read(65536), 5)
        if not chunk:
            break
        got += chunk
    writer.close()
    ok = got == payload
    print(f"  [test] {label}: {'OK' if ok else 'FAIL'} "
          f"(sent {len(payload)}B, got {len(got)}B)", flush=True)
    return ok


async def main():
    orca = await start_fake_orca()
    print("[test] 假 Orca echo 已启动", flush=True)

    relay = await run_proc("relay", os.path.join(HERE, "relay.py"),
                           "--public-port", str(PUBLIC_PORT),
                           "--ctrl-port", str(CTRL_PORT), "--token", TOKEN)
    await asyncio.sleep(0.5)
    gw = await run_proc("gateway", os.path.join(HERE, "gateway.py"),
                        "--relay-host", "127.0.0.1", "--ctrl-port", str(CTRL_PORT),
                        "--local-port", str(ORCA_PORT), "--token", TOKEN)
    await asyncio.sleep(1.0)  # 等 gateway 建好控制连接

    results = []
    # 1) 小数据往返
    results.append(await phone_roundtrip("small", b"hello orca mobile"))
    # 2) 大数据往返(64KB * 4,验证多次 read/drain)
    results.append(await phone_roundtrip("large", os.urandom(256 * 1024)))
    # 3) 并发 5 个"手机"
    conc = await asyncio.gather(*[
        phone_roundtrip(f"concurrent-{i}", os.urandom(32 * 1024)) for i in range(5)
    ])
    results.extend(conc)

    relay.terminate()
    gw.terminate()
    orca.close()
    await asyncio.sleep(0.2)

    passed = sum(results)
    print(f"\n[test] 结果:{passed}/{len(results)} 通过", flush=True)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
