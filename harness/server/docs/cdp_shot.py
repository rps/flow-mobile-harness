"""Screenshot a URL with headless Chrome over the DevTools protocol (stdlib only).

usage: cdp_shot.py <out.png> <width> <height> <light|dark> <url> [wait_s] [full]
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


class WS:
    def __init__(self, url):
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        assert b" 101 " in buf.split(b"\r\n")[0], buf
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, method, **params):
        self.n += 1
        data = json.dumps({"id": self.n, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        ln = len(data)
        if ln < 126:
            header += bytes([0x80 | ln])
        elif ln < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        self.s.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            msg = self.recv()
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def recv(self):
        b1, b2 = self._read(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(ln)
        if (b1 & 0x0F) == 0x8:
            raise EOFError("closed")
        return json.loads(payload)


def main():
    out, width, height, theme, url = sys.argv[1:6]
    wait_s = float(sys.argv[6]) if len(sys.argv) > 6 else 3.0
    full = len(sys.argv) > 7 and sys.argv[7] == "full"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                             f"--remote-debugging-port={PORT}", "--user-data-dir=/tmp/cdp-shot-profile",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                break
            except Exception:
                time.sleep(0.1)
        page = next(t for t in targets if t["type"] == "page")
        ws = WS(page["webSocketDebuggerUrl"])
        ws.send("Emulation.setDeviceMetricsOverride", width=int(width), height=int(height),
                deviceScaleFactor=1, mobile=int(width) < 600)
        ws.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme}])
        ws.send("Page.navigate", url=url)
        time.sleep(wait_s)
        params = {"format": "png"}
        if full:
            metrics = ws.send("Page.getLayoutMetrics")
            h = int(metrics["cssContentSize"]["height"])
            ws.send("Emulation.setDeviceMetricsOverride", width=int(width), height=h,
                    deviceScaleFactor=1, mobile=int(width) < 600)
            time.sleep(0.5)
        shot = ws.send("Page.captureScreenshot", **params)
        scroll_w = ws.send("Runtime.evaluate", expression="document.documentElement.scrollWidth", returnByValue=True)
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(f"{out}: {width}x{height} {theme}; document scrollWidth={scroll_w['result']['value']}")
    finally:
        proc.terminate()
        proc.wait(5)


if __name__ == "__main__":
    main()
