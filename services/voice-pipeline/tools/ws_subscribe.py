#!/usr/bin/env python3
# 纯标准库 WebSocket 订阅端: 实时显示流水线事件(无需安装任何依赖)
# 用法: python3 ws_subscribe.py [ws://HOST:PORT]   (默认 ws://127.0.0.1:8765)
import base64, json, os, socket, sys, time
from urllib.parse import urlparse


def connect(url):
    u = urlparse(url)
    host, port, path = u.hostname, u.port or 80, (u.path or "/")
    s = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("握手阶段连接被关闭")
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    status = head.decode(errors="replace").split("\r\n")[0]
    if "101" not in status:
        raise RuntimeError(f"握手失败: {status}")
    s.settimeout(None)   # 握手成功后永久阻塞, 直到服务端关闭或用户 Ctrl-C
    return s, rest


def recv_exact(s, n, buf):
    while len(buf) < n:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("连接已关闭")
        buf += chunk
    return buf[:n], buf[n:]


def send_pong(s, payload):
    mask = os.urandom(4)
    s.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask +
              bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def frames(s, buf):
    while True:
        hdr, buf = recv_exact(s, 2, buf)
        opcode = hdr[0] & 0x0F
        if opcode == 8:
            return
        ln = hdr[1] & 0x7F
        if ln == 126:
            ext, buf = recv_exact(s, 2, buf); ln = int.from_bytes(ext, "big")
        elif ln == 127:
            ext, buf = recv_exact(s, 8, buf); ln = int.from_bytes(ext, "big")
        if hdr[1] & 0x80:
            _mask, buf = recv_exact(s, 4, buf)
        payload, buf = recv_exact(s, ln, buf)
        if opcode == 9:
            send_pong(s, payload)
            continue
        if opcode in (1, 2):
            yield payload


def render(ev):
    t = ev.get("type")
    if t == "turn":
        return (f'说话人 {ev.get("speaker")}  [{ev.get("start")}–{ev.get("end")}s]  '
                f'{ev.get("text", "")}')
    if t == "summary":
        if ev.get("status") == "ok":
            return f"──── 会议总结(LLM) ────\n{ev.get('text', '')}\n────────────────────"
        return f"会议总结: {ev.get('status')} — {ev.get('reason', '')}"
    return json.dumps(ev, ensure_ascii=False)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765"
    s, buf = connect(url)
    print(f"[ws] 已连接 {url} — 等待事件, Ctrl-C 退出", flush=True)
    try:
        for p in frames(s, buf):
            txt = p.decode("utf-8", errors="replace")
            try:
                ev = json.loads(txt)
            except Exception:
                ev = {"type": "raw", "raw": txt}
            print(f"[{time.strftime('%H:%M:%S')}] {render(ev)}", flush=True)
        print("[ws] 服务端已关闭连接(会话结束)")
    except KeyboardInterrupt:
        print("\n[ws] 已退出")
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        print(f"\n[ws] 连接断开: {e}")


if __name__ == "__main__":
    main()
