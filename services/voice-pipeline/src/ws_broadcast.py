#!/usr/bin/env python3
"""最小 WebSocket 广播服务器(纯标准库, RFC 6455)。

把实时流水线的 JSONL 事件广播给所有订阅者。只订阅语义:
- 客户端完成握手后即收到之后产生的全部事件(text frame, 每帧一个 JSONL 事件);
- 服务器不消费客户端发来的 text/binary 帧;支持 ping/pong 保活与 close 握手;
- 无鉴权, 面向局域网工具场景, 不要把端口暴露到公网。

兼容任何标准 WebSocket 客户端(websocat、浏览器 WebSocket、python websockets 等)。

三种运行形态:
1. stdin 桥接(默认): stdin 每行 → 广播一帧; EOF 后关流退出。
2. 守护模式(--daemon): 生命周期与流水线解耦, 由外部进程经控制端口
   (默认 127.0.0.1:ws端口+1, 每连接一条 JSON 命令)驱动:
     {"cmd":"ping"}              → {"ok":true,"clients":N}   (就绪探测)
     {"cmd":"emit","line":str}   → 广播一条 JSONL 事件(如 summary 事件)
     {"cmd":"close"}             → 回复 ok 后向所有订阅者发 close 帧并退出
   idle_timeout>0 时, 超过该秒数无任何 emit/close 自动退出, 防止孤儿进程。
3. 控制客户端(--send-control): 向控制端口发一条命令, 打印响应后退出。
"""
import base64
import hashlib
import json
import signal
import socket
import struct
import sys
import threading
import time

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _log(*args):
    print(*args, file=sys.stderr, flush=True)


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    return header + payload


def control_request(host: str, port: int, obj: dict, timeout: float = 8.0) -> dict:
    """向控制端口发送一条 JSON 命令, 返回解析后的响应 dict。失败抛 OSError/ValueError。"""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < 1048576:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    return json.loads(line) if line else {}


class ControlPublisher:
    """流水线侧发布器: 经控制端口把事件投递给独立广播 daemon。

    与 WebSocketBroker 鸭子兼容(都有 publish(line))。控制端口不可达时
    丢弃事件并告警, 绝不让广播故障影响转录主流程。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8766, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._fails = 0

    @staticmethod
    def probe(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            return control_request(host, port, {"cmd": "ping"}, timeout=timeout).get("ok") is True
        except (OSError, ValueError):
            return False

    def publish(self, line: str):
        try:
            control_request(self.host, self.port, {"cmd": "emit", "line": line},
                            timeout=self.timeout)
            if self._fails:
                _log(f"[ws] 控制通道已恢复(此前连续失败 {self._fails} 次)")
            self._fails = 0
        except (OSError, ValueError) as e:
            self._fails += 1
            if self._fails <= 3 or self._fails % 20 == 0:
                _log(f"[ws] ⚠ 控制端口发布失败(连续 {self._fails} 次): {e}")


class WebSocketBroker:
    """单端口广播。publish() 线程安全;发送失败的客户端自动移除。"""

    def __init__(self, host="0.0.0.0", port=8765, max_clients=32,
                 control_host="127.0.0.1", control_port=0, idle_timeout=0.0):
        self.host = host
        self.port = port
        self.max_clients = max_clients
        self.control_host = control_host
        self.control_port = control_port
        self.idle_timeout = idle_timeout
        self._clients = {}   # socket -> True
        self._lock = threading.Lock()
        self._server = None
        self._ctrl_server = None
        self._stopping = threading.Event()
        self._closed = threading.Event()
        self._last_activity = time.time()

    # ── 生命周期 ──
    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        if self.control_port > 0:
            self._ctrl_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._ctrl_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._ctrl_server.bind((self.control_host, self.control_port))
            self._ctrl_server.listen(4)
            threading.Thread(target=self._control_loop, name="ws-control", daemon=True).start()
        if self.idle_timeout > 0:
            threading.Thread(target=self._idle_watchdog, name="ws-idle", daemon=True).start()
        threading.Thread(target=self._accept_loop, name="ws-accept", daemon=True).start()
        ctrl = f" (控制端口 {self.control_host}:{self.control_port})" if self.control_port > 0 else ""
        _log(f"[ws] 广播服务已启动: ws://{self.host}:{self.port}{ctrl}")

    def stop(self):
        self._closed.set()
        self._stopping.set()
        for srv in (self._ctrl_server, self._server):
            if srv:
                try:
                    srv.close()
                except OSError:
                    pass
        with self._lock:
            conns = list(self._clients)
            self._clients.clear()
        for conn in conns:
            try:
                conn.sendall(_encode_frame(b"", opcode=0x8))
                conn.close()
            except OSError:
                pass

    def wait_closed(self, timeout=None):
        """阻塞直到 close 命令 / idle 超时 / SIGTERM。返回是否已关闭。"""
        return self._closed.wait(timeout)

    def client_count(self):
        with self._lock:
            return len(self._clients)

    # ── 广播 ──
    def publish(self, line: str):
        self._last_activity = time.time()
        frame = _encode_frame(line.encode("utf-8"), opcode=0x1)
        dead = []
        with self._lock:
            conns = list(self._clients)
        for conn in conns:
            try:
                conn.sendall(frame)
            except OSError:
                dead.append(conn)
        if dead:
            self._drop(dead)

    def _drop(self, conns):
        with self._lock:
            for conn in conns:
                self._clients.pop(conn, None)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass

    # ── 控制端口 ──
    def _control_loop(self):
        while not self._stopping.is_set():
            try:
                conn, _addr = self._ctrl_server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_control, args=(conn,),
                             name="ws-control-conn", daemon=True).start()

    def _handle_control(self, conn):
        do_close = False
        try:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf and len(buf) < 4 * 1024 * 1024:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            req = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            self._last_activity = time.time()
            resp = {"ok": False, "error": "empty command"}
            if req:
                try:
                    cmd = json.loads(req)
                    op = cmd.get("cmd")
                    if op == "ping":
                        resp = {"ok": True, "clients": self.client_count()}
                    elif op == "emit" and isinstance(cmd.get("line"), str) and cmd["line"].strip():
                        self.publish(cmd["line"].strip())
                        resp = {"ok": True, "clients": self.client_count()}
                    elif op == "close":
                        resp = {"ok": True}
                        do_close = True
                    else:
                        resp = {"ok": False, "error": f"unknown cmd: {op!r}"}
                except Exception as e:
                    resp = {"ok": False, "error": str(e)[:200]}
            try:
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            except OSError:
                pass
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if do_close:
                _log("[ws] 收到控制端口 close 命令, 关闭广播服务")
                self.stop()

    def _idle_watchdog(self):
        while not self._closed.is_set():
            time.sleep(5)
            if self.idle_timeout > 0 and time.time() - self._last_activity > self.idle_timeout:
                _log(f"[ws] {self.idle_timeout:.0f}s 无事件, 自动退出(idle timeout)")
                self.stop()
                return

    # ── 连接处理 ──
    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                conn, addr = self._server.accept()
            except OSError:
                break
            if self.client_count() >= self.max_clients:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle_client, args=(conn, addr),
                             name=f"ws-client-{addr[1]}", daemon=True).start()

    def _handle_client(self, conn, addr):
        try:
            if not self._handshake(conn):
                conn.close()
                return
            with self._lock:
                self._clients[conn] = True
            _log(f"[ws] 订阅者接入: {addr[0]}:{addr[1]} (当前 {self.client_count()})")
            self._read_loop(conn)
        except (ConnectionError, OSError):
            pass
        finally:
            self._drop([conn])
            _log(f"[ws] 订阅者断开: {addr[0]}:{addr[1]} (当前 {self.client_count()})")

    def _handshake(self, conn) -> bool:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            data += chunk
            if len(data) > 16384:
                return False
        headers = {}
        for line in data.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        ws_key = headers.get(b"sec-websocket-key", b"").decode()
        if not ws_key:
            try:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            return False
        accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        return True

    def _read_loop(self, conn):
        while not self._stopping.is_set():
            hdr = _recv_exact(conn, 2)
            opcode = hdr[0] & 0x0F
            masked = hdr[1] & 0x80
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", _recv_exact(conn, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", _recv_exact(conn, 8))[0]
            mask = _recv_exact(conn, 4) if masked else b"\x00" * 4
            payload = _recv_exact(conn, length) if length else b""
            if masked and payload:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                try:
                    conn.sendall(_encode_frame(b"", opcode=0x8))
                except OSError:
                    pass
                return
            if opcode == 0x9:  # ping → pong
                conn.sendall(_encode_frame(payload, opcode=0xA))
            # text / binary / pong: 忽略


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="WebSocket 广播: stdin 桥接(默认) / --daemon 守护 / --send-control 控制客户端")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--control-host", default="127.0.0.1")
    ap.add_argument("--control-port", type=int, default=0,
                    help="控制端口; 0=不启用(默认 ws 端口+1 由 run.sh 指定)")
    ap.add_argument("--idle-timeout", type=float, default=0.0,
                    help="秒; 超过该时长无任何 emit/close 自动退出; 0=不启用")
    ap.add_argument("--daemon", action="store_true",
                    help="守护模式: 不读 stdin, 等待控制端口 close / idle 超时 / SIGTERM")
    ap.add_argument("--send-control", action="store_true",
                    help="客户端模式: 向 --control-port 发送一条命令后退出")
    ap.add_argument("--cmd", default="ping", choices=["ping", "emit", "close"])
    ap.add_argument("--line", default="", help="--cmd emit 时广播的 JSONL 事件")
    args = ap.parse_args()

    if args.send_control:
        obj = {"cmd": args.cmd}
        if args.cmd == "emit":
            if not args.line.strip():
                print("emit 需要 --line", file=sys.stderr)
                sys.exit(2)
            obj["line"] = args.line.strip()
        try:
            resp = control_request(args.control_host, args.control_port, obj)
        except (OSError, ValueError) as e:
            print(json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps(resp, ensure_ascii=False))
        sys.exit(0 if resp.get("ok") else 1)

    broker = WebSocketBroker(args.host, args.port,
                             control_host=args.control_host,
                             control_port=args.control_port,
                             idle_timeout=args.idle_timeout)
    broker.start()
    if args.daemon:
        signal.signal(signal.SIGTERM, lambda *_: broker.stop())
        try:
            broker.wait_closed()
        except KeyboardInterrupt:
            pass
        finally:
            broker.stop()
    else:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    broker.publish(line)
        except KeyboardInterrupt:
            pass
        finally:
            broker.stop()
