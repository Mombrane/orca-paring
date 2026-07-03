#!/usr/bin/env python3
# Orca Mobile 反向隧道 —— 用户管理(admin)· 跑在云服务器上
#
# 维护 users.json,并在变更后给 relay 发 SIGHUP 触发热重载(不断开老用户)。
#
#   admin.py add <name>        新增用户,自动分配端口(7101-7150),生成 token
#   admin.py list              列出所有用户(端口 / token 掩码 / 是否在线由 relay 日志看)
#   admin.py show <name>       显示某用户完整信息(含 token,用于发给用户)
#   admin.py remove <name>     删除用户
#   admin.py reload            仅发 SIGHUP 重载(手动改了 json 后用)
#
# 纯标准库,python3.8+。

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys

USERS_PATH = os.environ.get("ORCA_USERS_PATH", "/opt/orca-relay/users.json")
PORT_MIN = 7101
PORT_MAX = 7150
RELAY_HOST_PUBLIC = os.environ.get("ORCA_RELAY_PUBLIC", "<RELAY_PUBLIC_IP>")


def load_users() -> dict:
    if not os.path.exists(USERS_PATH):
        return {}
    with open(USERS_PATH) as f:
        return json.load(f)


def save_users(users: dict):
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_PATH)
    os.chmod(USERS_PATH, 0o600)


def alloc_port(users: dict) -> int:
    used = {int(v["port"]) for v in users.values()}
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p not in used:
            return p
    print(f"错误:端口段 {PORT_MIN}-{PORT_MAX} 已用尽({len(used)} 个)。", file=sys.stderr)
    sys.exit(1)


def signal_relay_reload():
    """给 relay 进程发 SIGHUP。用 systemd 时用 systemctl kill -s HUP 更稳。"""
    # 优先走 systemd
    r = subprocess.run(["systemctl", "kill", "-s", "HUP", "orca-relay"],
                       capture_output=True)
    if r.returncode == 0:
        print("已通知 relay 热重载(systemctl kill -s HUP)。")
        return
    # 回退:按进程名找
    try:
        out = subprocess.run(["pgrep", "-f", "relay.py"], capture_output=True)
        pids = out.stdout.decode().split()
        for pid in pids:
            os.kill(int(pid), signal.SIGHUP)
        if pids:
            print(f"已向 relay 进程 {pids} 发送 SIGHUP。")
        else:
            print("警告:未找到运行中的 relay 进程,重载未生效。", file=sys.stderr)
    except Exception as e:
        print(f"警告:发送 SIGHUP 失败:{e}", file=sys.stderr)


def mask(tok: str) -> str:
    return tok[:6] + "…" + tok[-4:] if len(tok) > 12 else "…"


def cmd_add(args):
    users = load_users()
    if args.name in users:
        print(f"用户 {args.name} 已存在:端口 {users[args.name]['port']}", file=sys.stderr)
        sys.exit(1)
    port = args.port or alloc_port(users)
    # 校验手动指定的端口
    if not (PORT_MIN <= port <= PORT_MAX):
        print(f"错误:端口需在 {PORT_MIN}-{PORT_MAX}", file=sys.stderr); sys.exit(1)
    if port in {int(v["port"]) for v in users.values()}:
        print(f"错误:端口 {port} 已被占用", file=sys.stderr); sys.exit(1)
    token = secrets.token_urlsafe(32)
    users[args.name] = {"token": token, "port": port}
    save_users(users)
    signal_relay_reload()
    print("=" * 60)
    print(f"已创建用户: {args.name}")
    print(f"  端口  : {port}")
    print(f"  token : {token}")
    print("=" * 60)
    print("把下面这段发给该用户,在他自己的 Mac 上操作:\n")
    print(f"# 1) 常驻运行网关(建议做成开机自启)")
    print(f"python3 gateway.py run --relay-host {RELAY_HOST_PUBLIC} \\")
    print(f"    --user {args.name} --token {token}\n")
    print(f"# 2) 在 Orca 生成手机配对码并复制后,改写为专属端口:")
    print(f"python3 gateway.py pair --relay-host {RELAY_HOST_PUBLIC} --port {port}")
    print("   (自动读剪贴板;输出的新配对码交给手机扫)")


def cmd_list(args):
    users = load_users()
    if not users:
        print("(无用户)"); return
    print(f"{'用户':<16}{'端口':<8}{'token(掩码)'}")
    print("-" * 44)
    for name, v in sorted(users.items(), key=lambda kv: kv[1]["port"]):
        print(f"{name:<16}{v['port']:<8}{mask(v['token'])}")


def cmd_show(args):
    users = load_users()
    u = users.get(args.name)
    if not u:
        print(f"用户 {args.name} 不存在", file=sys.stderr); sys.exit(1)
    print(json.dumps({args.name: u}, indent=2, ensure_ascii=False))


def cmd_remove(args):
    users = load_users()
    if args.name not in users:
        print(f"用户 {args.name} 不存在", file=sys.stderr); sys.exit(1)
    del users[args.name]
    save_users(users)
    signal_relay_reload()
    print(f"已删除用户 {args.name} 并重载 relay。")


def cmd_reload(args):
    signal_relay_reload()


def main():
    ap = argparse.ArgumentParser(description="Orca Mobile 反向隧道 —— 用户管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增用户")
    a.add_argument("name")
    a.add_argument("--port", type=int, default=None, help="手动指定端口(默认自动分配)")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="列出用户").set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="显示某用户完整信息")
    s.add_argument("name"); s.set_defaults(func=cmd_show)

    rm = sub.add_parser("remove", help="删除用户")
    rm.add_argument("name"); rm.set_defaults(func=cmd_remove)

    sub.add_parser("reload", help="仅发 SIGHUP 重载").set_defaults(func=cmd_reload)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
