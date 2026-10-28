"""Run inside an empty network namespace: every DNS query the browser makes is a connection that
did not go through the proxy. Usage: probe.py <outdir> [strict]"""
import asyncio, json, os, socket, sys, threading, time

OUT = sys.argv[1]
NAMESERVER = next((l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")), "127.0.0.53")
STRICT = len(sys.argv) > 2
dns, proxied, direct = [], [], []


def dns_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((NAMESERVER, 53))
    while True:
        data, addr = s.recvfrom(512)
        i, labels = 12, []
        while i < len(data) and data[i]:
            labels.append(data[i + 1:i + 1 + data[i]].decode("ascii", "replace"))
            i += data[i] + 1
        dns.append(".".join(labels))
        s.sendto(data[:2] + b"\x81\x83" + data[4:6] + b"\x00\x00\x00\x00\x00\x00" + data[12:i + 5], addr)


PAGE = b"""<html><head><title>leak</title>
<link rel=dns-prefetch href="//dnsprefetch.leak.test"><link rel=preconnect href="https://preconnect.leak.test">
<link rel=prefetch href="http://prefetch.leak.test/x"><link rel=icon href="http://favicon.leak.test/f.ico">
</head><body><img src="http://img.leak.test/a.png">
<script>
try { new WebSocket("ws://ws.leak.test/s"); } catch (e) {}
try { const pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.leak.test:3478"}]});
      pc.createDataChannel("x"); pc.createOffer().then(o => pc.setLocalDescription(o)); } catch (e) {}
fetch("http://fetch.leak.test/api").catch(() => {});
fetch("http://127.0.0.1:18081/local").catch(() => {});
</script></body></html>"""


async def proxy(reader, writer):
    try:
        line = (await reader.readline()).decode("latin1")
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        method, target = line.split(" ")[:2]
        proxied.append(f"{method} {target}")
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n%s"
                         % (len(PAGE), PAGE))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def main():
    threading.Thread(target=dns_server, daemon=True).start()
    server = await asyncio.start_server(proxy, "127.0.0.1", 18080)

    async def local(reader, writer):
        direct.append((await reader.readline()).decode("latin1").strip())
        writer.close()
    local_server = await asyncio.start_server(local, "127.0.0.1", 18081)
    from hutch.pool import Pool
    async with Pool(base_dir=os.path.join(OUT, "profiles")) as pool:
        s = await pool.create("leak", headless=True, proxy="http://127.0.0.1:18080", strict_proxy=STRICT)
        await s.goto("http://page.leak.test/")
        await asyncio.sleep(25)
        hosts = [h["host"] for h in s.hosts()]
    server.close()
    json.dump({"dns": sorted(set(dns)), "proxied": sorted(set(proxied)), "direct": direct, "hosts": hosts},
              open(os.path.join(OUT, "strict.json" if STRICT else "default.json"), "w"), indent=1)


asyncio.run(main())
