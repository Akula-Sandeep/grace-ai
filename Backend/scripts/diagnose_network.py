"""Diagnose *where* a provider host is unreachable: DNS, TCP, or TLS/app layer.

Run this on your own machine (not in a container or CI), once with your VPN
off and once with it on, and compare the two runs:

    cd Backend && python scripts/diagnose_network.py --label "vpn-off"
    cd Backend && python scripts/diagnose_network.py --label "vpn-on"

It makes no provider API calls and needs no credentials -- it only resolves
each host's name and opens a plain TCP/TLS connection to it, the same way
any browser or HTTP client would before sending a request. It never tries to
go around a block: no proxy, no alternate route is used, only diagnosis.

For each host it reports, independently:
  - system DNS  : what your configured resolver returns
  - public DNS   : what Cloudflare's and Google's public DoH resolvers
                    return for the same name (same protocol family as a
                    browser's own DNS-over-HTTPS, not a traffic proxy)
  - TCP connect  : whether a bare TCP connection to port 443 on each
                    system-DNS address succeeds, and how long it takes
  - TLS + HTTP   : whether a real HTTPS request completes end to end

Reading the result:
  - system DNS empty/differs from public DNS, TCP also fails
        -> likely DNS interference (a resolver returning no/wrong records).
           A different DNS resolver (configured at the OS or router level,
           not in this app) may fix it without needing a VPN at all.
  - system DNS matches public DNS, but TCP connect fails or times out
        -> likely a network-path/firewall block (ISP or in between), not a
           DNS problem. Only routing around it (VPN, a different network)
           helps; changing DNS will not.
  - TCP connects but TLS/HTTP fails
        -> an inline inspection/block keyed on the destination, not DNS or a
           raw IP block.
  - Works in one run and not the other (e.g. vpn-off fails, vpn-on passes
    for one host but a *different* host regresses) -> the two networks
    disagree on what they allow; nothing in this application can reconcile
    that for both hosts in the same process, since it is not a client-side
    choice -- it is enforced by your ISP and/or VPN provider's own policy.
    The practical options are then: route only the blocked host(s) through
    whichever network reaches them (split tunneling in the VPN client, a
    custom per-domain route), or accept using each capability from whichever
    network reaches it.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field

import httpx

HOSTS = [
    "api.themoviedb.org",
    "image.tmdb.org",
    "api.groq.com",
    "generativelanguage.googleapis.com",
    "query.wikidata.org",
    "www.wikidata.org",
    "musicbrainz.org",
]
TCP_TIMEOUT = 5.0
HTTP_TIMEOUT = 8.0


@dataclass
class HostReport:
    host: str
    system_dns: list[str] = field(default_factory=list)
    system_dns_error: str | None = None
    public_dns: dict[str, list[str] | str] = field(default_factory=dict)
    tcp: list[dict] = field(default_factory=list)
    https: dict | None = None
    verdict: str = ""


def resolve_system(host: str) -> tuple[list[str], str | None]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        addrs = sorted({info[4][0] for info in infos})
        return addrs, None
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc}"


def resolve_public_doh(host: str, client: httpx.Client, url: str, params: dict) -> list[str] | str:
    try:
        response = client.get(url, params=params, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("Answer") or []
        addrs = sorted({a["data"] for a in answers if a.get("type") in (1, 28)})  # A, AAAA
        return addrs
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def tcp_connect(host: str, address: str) -> dict:
    started = time.monotonic()
    try:
        with socket.create_connection((address, 443), timeout=TCP_TIMEOUT):
            return {"address": address, "ok": True, "seconds": round(time.monotonic() - started, 2)}
    except OSError as exc:
        return {
            "address": address,
            "ok": False,
            "seconds": round(time.monotonic() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def https_probe(host: str) -> dict:
    started = time.monotonic()
    try:
        context = ssl.create_default_context()
        with (
            socket.create_connection((host, 443), timeout=TCP_TIMEOUT) as sock,
            context.wrap_socket(sock, server_hostname=host) as tls,
        ):
            tls.settimeout(HTTP_TIMEOUT)
            tls.send(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            tls.recv(256)
        return {"ok": True, "seconds": round(time.monotonic() - started, 2)}
    except Exception as exc:
        return {
            "ok": False,
            "seconds": round(time.monotonic() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def verdict_for(report: HostReport) -> str:
    system_ok = bool(report.system_dns)
    public_addrs = [v for v in report.public_dns.values() if isinstance(v, list) and v]
    public_ok = bool(public_addrs)
    tcp_ok = any(entry.get("ok") for entry in report.tcp)
    https_ok = bool(report.https and report.https.get("ok"))

    if https_ok:
        return "OK: resolves and connects end to end."
    if not system_ok and public_ok:
        return "DNS interference likely: your resolver returned nothing, but public DNS has records."
    if system_ok and public_ok:
        system_set, public_set = set(report.system_dns), set().union(*public_addrs) if public_addrs else set()
        if system_set and public_set and not (system_set & public_set):
            return "DNS mismatch: your resolver's addresses do not overlap with public DNS at all."
    if system_ok and not tcp_ok:
        return "Network-path block likely: DNS resolves, but no TCP connection on port 443 succeeds."
    if tcp_ok and not https_ok:
        return "TLS/application-layer block likely: TCP connects, but the HTTPS handshake/request fails."
    if not system_ok and not public_ok:
        return "No DNS resolution from any resolver tried; check general connectivity."
    return "Unclear: see the raw fields above."


def diagnose(host: str, doh_client: httpx.Client) -> HostReport:
    report = HostReport(host=host)
    report.system_dns, report.system_dns_error = resolve_system(host)
    report.public_dns["cloudflare"] = resolve_public_doh(
        host,
        doh_client,
        "https://cloudflare-dns.com/dns-query",
        {"name": host, "type": "A"},
    )
    report.public_dns["google"] = resolve_public_doh(
        host, doh_client, "https://dns.google/resolve", {"name": host, "type": "A"}
    )
    for address in report.system_dns[:3]:
        report.tcp.append(tcp_connect(host, address))
    if report.system_dns:
        report.https = https_probe(host)
    report.verdict = verdict_for(report)
    return report


def main(label: str | None) -> int:
    headers = {"accept": "application/dns-json"}
    with httpx.Client(headers=headers) as doh_client:
        results = [diagnose(host, doh_client) for host in HOSTS]

    print(f"=== Network diagnosis{f' ({label})' if label else ''} ===")
    for report in results:
        print(f"\n{report.host}")
        print(f"  system DNS : {report.system_dns or report.system_dns_error}")
        print(f"  public DNS : {report.public_dns}")
        for entry in report.tcp:
            status = "ok" if entry["ok"] else entry.get("error")
            print(f"  tcp {entry['address']:<20} {entry['seconds']:>5.2f}s  {status}")
        if report.https is not None:
            status = "ok" if report.https["ok"] else report.https.get("error")
            print(f"  https probe: {report.https['seconds']:.2f}s  {status}")
        print(f"  verdict    : {report.verdict}")

    print("\n--- JSON (for sharing/comparison) ---")
    print(json.dumps([asdict(r) for r in results], indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", help="A short tag for this run, e.g. 'vpn-off', printed in the output.")
    args = parser.parse_args()
    sys.exit(main(args.label))
