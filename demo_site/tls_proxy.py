"""HTTPS for the demo on the host's tailnet addresses, without root: a self-signed stand-in
for Tailscale Serve (DEMO-README.md, "On the tailnet without root").

The web listens on 127.0.0.1 only and wants an HTTPS origin. This front terminates TLS on the
addresses it is given, which must be tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) or loopback
addresses: never a wildcard or a LAN address. The web answers one request per connection
(HTTP/1.0), so the front reads one request head per connection and passes it on with every
X-Forwarded-For replaced by one naming the peer, as Tailscale's proxy does: the web's
per-client login limit (--trust-funnel) then sees each person, not this front. A peer named
with --trusted-proxy is a front door that does the same for its own visitors (the public
link's reverse proxy, on the tailnet): from it, one X-Forwarded-For naming one address is
kept, and anything else is replaced by the peer as usual. Every other byte goes through
unchanged, both ways. A head the web might split into lines differently (a bare CR or LF, a
folded or malformed header line) is refused, so no second X-Forwarded-For gets past. Nothing
is logged per connection.

  python3 -m demo_site.tls_proxy serve --listen ADDRESS:PORT [--listen ...] --cert C --key K
      [--upstream-port 8765] [--trusted-proxy ADDRESS ...]
  python3 -m demo_site.tls_proxy cert --host NAME --cert C --key K

`cert` writes a self-signed certificate that browsers let a person accept: ECDSA P-256 with
SHA-256, NAME as its only subjectAltName, serverAuth, CA:FALSE, valid 397 days (macOS and iOS
refuse a TLS server certificate valid for more than 825). It prints the SHA-256 fingerprint,
for people to compare with the one their browser shows.
"""
import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import tempfile

HEAD_LIMIT = 32 * 1024
HEAD_SECONDS = 15
IDLE_SECONDS = 300
MAX_CONNECTIONS = 64
CERT_DAYS = 397
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
HEADER_LINE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:")
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME = re.compile(r"(?=.{1,253}\Z)(?:%s\.)+%s\Z" % (LABEL, LABEL))
REASONS = {400: b"Bad Request", 503: b"Service Unavailable"}


def tailnet_address(text):
    """A tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) or loopback address, else ValueError."""
    address = ipaddress.ip_address(text)
    if not (address.is_loopback or any(address in net for net in TAILNET)):
        raise ValueError("%s is not a tailnet or loopback address" % address)
    return address


def listen_address(text):
    """'100.64.0.1:8443' or '[fd7a:115c:a1e0::1]:8443' as (address, port), for a tailnet or
    loopback address only."""
    host, _, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("listen on ADDRESS:PORT, not %r" % text) from None
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("listen on ADDRESS:PORT, not %r" % text)
    return str(tailnet_address(host)), int(port)


def forwarded_head(head, peer, trusted=False):
    """A request head, through its blank line, with every X-Forwarded-For line dropped and one
    naming the client added: peer, or, where peer is a trusted front door, the one address its
    single X-Forwarded-For names. None for a head the web might split into lines differently."""
    if not head.endswith(b"\r\n\r\n"):
        return None
    lines = head[:-4].split(b"\r\n")
    if not lines[0] or any(b"\r" in line or b"\n" in line for line in lines):
        return None
    if not all(HEADER_LINE.match(line) for line in lines[1:]):
        return None
    named = [line.split(b":", 1)[1].strip() for line in lines[1:]
             if line.split(b":", 1)[0].lower() == b"x-forwarded-for"]
    client = peer
    if trusted and len(named) == 1:
        try:
            client = str(ipaddress.ip_address(named[0].decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            pass
    kept = [line for line in lines[1:] if line.split(b":", 1)[0].lower() != b"x-forwarded-for"]
    return b"\r\n".join([lines[0]] + kept + [b"X-Forwarded-For: " + client.encode("ascii")]) + b"\r\n\r\n"


async def answer(writer, status, message):
    body = json.dumps({"error": message}).encode()
    writer.write(b"HTTP/1.0 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
                 b"Content-Length: %d\r\nCache-Control: no-store\r\n\r\n%s"
                 % (status, REASONS[status], len(body), body))
    await writer.drain()


async def pump(reader, writer, moved, idle=None):
    """One way until its reader ends. ``moved`` holds when either way last moved a byte; with
    ``idle``, this way gives up once neither has for that long. So an upload the web is still
    reading keeps open the way its answer will come, however long the upload takes."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await asyncio.wait_for(reader.read(65536),
                                          None if idle is None else moved[0] + idle - loop.time())
        except asyncio.TimeoutError:
            if loop.time() < moved[0] + idle:
                continue  # The other way moved meanwhile.
            raise
        if not data:
            return
        moved[0] = loop.time()
        writer.write(data)
        await writer.drain()


async def splice(reader, writer, up_reader, up_writer):
    """Both ways until the web closes, which it does after its one answer. A client that
    closes its sending side first has that passed on, and still gets the answer. A connection
    on which nothing has moved either way for IDLE_SECONDS is closed."""
    moved = [asyncio.get_running_loop().time()]
    upward = asyncio.ensure_future(pump(reader, up_writer, moved))
    downward = asyncio.ensure_future(pump(up_reader, writer, moved, IDLE_SECONDS))
    try:
        await asyncio.wait({upward, downward}, return_when=asyncio.FIRST_COMPLETED)
        if not downward.done() and not upward.cancelled() and upward.exception() is None:
            if up_writer.can_write_eof():
                up_writer.write_eof()
            await asyncio.wait({downward})
    finally:
        for task in (upward, downward):
            task.cancel()
        await asyncio.gather(upward, downward, return_exceptions=True)


async def close(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 5)
    except (OSError, TimeoutError):
        pass


class Front:
    def __init__(self, upstream_port, trusted=()):
        self.upstream_port = upstream_port
        self.trusted = frozenset(tailnet_address(str(a)) for a in trusted)
        self.active = 0

    async def handle(self, reader, writer):
        if self.active >= MAX_CONNECTIONS:
            writer.transport.abort()
            return
        self.active += 1
        upstream = []
        try:
            await self.relay(reader, writer, upstream)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            self.active -= 1
            for each in upstream + [writer]:
                await close(each)

    async def relay(self, reader, writer, upstream):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
        except asyncio.LimitOverrunError:
            return await answer(writer, 400, "Request rejected.")
        peer = ipaddress.ip_address(writer.get_extra_info("peername")[0].split("%")[0])
        head = forwarded_head(head, str(peer), peer in self.trusted)
        if head is None:
            return await answer(writer, 400, "Request rejected.")
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.upstream_port), 10)
        except (OSError, TimeoutError):
            return await answer(writer, 503, "The demo service is unavailable. Please try again.")
        upstream.append(up_writer)
        up_writer.write(head)
        await splice(reader, writer, up_reader, up_writer)


def tls_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    context.set_alpn_protocols(["http/1.1"])
    return context


async def serve(listens, cert, key, upstream_port, trusted=()):
    context = tls_context(cert, key)
    front = Front(upstream_port, trusted)
    servers = [await asyncio.start_server(front.handle, host, port, ssl=context, limit=HEAD_LIMIT,
                                          ssl_handshake_timeout=HEAD_SECONDS, reuse_address=True)
               for host, port in listens]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    where = ", ".join(("[%s]:%d" if ":" in host else "%s:%d") % (host, port) for host, port in listens)
    print("HTTPS on %s for 127.0.0.1:%d%s" % (where, upstream_port, "".join(
        "; X-Forwarded-For kept from %s" % a for a in sorted(map(str, front.trusted)))), flush=True)
    await stop.wait()
    for server in servers:
        server.close()


def fingerprint(cert):
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text())
    return ":".join("%02X" % b for b in hashlib.sha256(der).digest())


def make_cert(host, cert, key, days=CERT_DAYS):
    if not HOSTNAME.match(host):
        raise ValueError("not a host name: %r" % host)
    for path in (cert, key):
        if os.path.lexists(path):
            raise FileExistsError("%s exists; remove it to replace the certificate" % path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = ("[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
              "[dn]\nCN = %s\n"
              "[ext]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature\n"
              "extendedKeyUsage = serverAuth\nsubjectAltName = DNS:%s\n"
              "subjectKeyIdentifier = hash\n") % (host, host)
    old = os.umask(0o077)
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".cnf") as fh:
            fh.write(config)
            fh.flush()
            subprocess.run(["openssl", "req", "-x509", "-new", "-config", fh.name, "-newkey", "ec",
                            "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-sha256",
                            "-days", str(days), "-keyout", str(key), "-out", str(cert)],
                           check=True, capture_output=True, stdin=subprocess.DEVNULL)
    finally:
        os.umask(old)
    os.chmod(key, 0o600)
    return fingerprint(cert)


def _listen_arg(text):
    try:
        return listen_address(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def _trusted_arg(text):
    try:
        return tailnet_address(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTTPS on tailnet addresses for the demo's "
                                                 "loopback web")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="run the front")
    run.add_argument("--listen", action="append", required=True, type=_listen_arg,
                     metavar="ADDRESS:PORT")
    run.add_argument("--cert", required=True)
    run.add_argument("--key", required=True)
    run.add_argument("--upstream-port", type=int, default=8765)
    run.add_argument("--trusted-proxy", action="append", default=[], type=_trusted_arg,
                     metavar="ADDRESS", help="a front door's tailnet address, whose one "
                                             "X-Forwarded-For address is kept (repeatable)")
    make = commands.add_parser("cert", help="write a self-signed certificate and print its "
                                            "SHA-256 fingerprint")
    make.add_argument("--host", required=True)
    make.add_argument("--cert", required=True)
    make.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    if args.command == "cert":
        print("SHA-256 fingerprint", make_cert(args.host, args.cert, args.key))
        return
    if os.geteuid() == 0:
        parser.error("the front must run as an unprivileged user")
    asyncio.run(serve(args.listen, args.cert, args.key, args.upstream_port, args.trusted_proxy))


if __name__ == "__main__":
    main()
