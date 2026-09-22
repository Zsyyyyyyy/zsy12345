#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_eastmoney_hosts.py —— 东财行情链路连通性探针（只读，排障用）

背景：东财行情分散在几个域名/主机上，且走 CDN、节点 IP 会轮换。云机房出口
（如阿里云杭州）出现过「push2 / push2delay 同时被断连（RemoteDisconnected，
TLS 握手成功但请求不返回任何字节）」而 futsseapi 正常的情况；也出现过反过来
的状态。一旦看板整屏「无数据」，先用本脚本判定到底是**哪一环**不可达，
再去改代码或换数据源，避免瞎猜。

探什么：
  ① 各域名 DNS 解析到的 IP（看清是不是解析到了奇怪的节点）
  ② HTTPS 与 HTTP(80) 两种协议各请求一次（区分「TLS/网络问题」与「IP 被屏蔽」）
  ③ 业务侧真实用到的接口路径：ulist 行情、日K、合约列表、品种表、以及新浪对照

用法（项目根目录）：
    venv/bin/python scripts/probe_eastmoney_hosts.py            # 完整探测
    venv/bin/python scripts/probe_eastmoney_hosts.py --timeout 5

服务器上（/opt/zsy12345）：
    /opt/zsy12345/venv/bin/python3 /opt/zsy12345/scripts/probe_eastmoney_hosts.py

结果怎么看：
  - push2* 全 FAIL、futsseapi OK  -> 实时行情会走降级通道（合约列表），看板有数据，
    但「昨收 / 当日增仓 / 当日增仓占比」为空（显示 --）；
  - futsseapi 也 FAIL             -> 东财按出口 IP 段整体屏蔽，需换数据源或让服务走代理；
  - HTTP 通而 HTTPS 不通          -> 本机到该 CDN 节点的 TLS/网络问题，可考虑 http 兜底；
  - 新浪 hq.sinajs.cn 403/456     -> 该出口 IP 被新浪拉黑（阿里云等机房 IP 常见）。

只读探测，不修改任何配置、不写库。
"""
import argparse
import os
import socket
import ssl
import time
import urllib.request

# 行情源都是国内站点，绕过本机可能存在的代理（与业务代码口径一致，否则会 ProxyError）
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 只取一个主力连续合约（纯碱主连），响应很短，够判断可达性
_PROBE_PATHS = {
    "push2.eastmoney.com": (
        "/api/qt/ulist.np/get?fltt=2&invt=2&np=1&secids=115.sam"
        "&fields=f2,f12,f14&ut=fa5fd1943c7b386f172d6893dbfba10b"
    ),
    "push2delay.eastmoney.com": (
        "/api/qt/ulist.np/get?fltt=2&invt=2&np=1&secids=115.sam"
        "&fields=f2,f12,f14&ut=fa5fd1943c7b386f172d6893dbfba10b"
    ),
    "push2his.eastmoney.com": (
        "/api/qt/stock/kline/get?secid=115.sam&klt=101&fqt=0&lmt=2&end=20500101"
        "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55&ut=fa5fd1943c7b386f172d6893dbfba10b"
    ),
    "futsseapi.eastmoney.com": (
        "/list/115?orderBy=dm&sort=asc&pageSize=3&pageIndex=0&field=dm,name,p,zjsj,ccl,utime"
    ),
    "futsse-static.eastmoney.com": "/redis?msgid=115",
    "hq.sinajs.cn": "/list=nf_SA2701",
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),                     # 显式不走代理
    urllib.request.HTTPSHandler(context=_CTX),
)


def probe(url: str, referer: str, timeout: int) -> str:
    """请求一次，返回一行人类可读的结果；异常一律压成「类型 + 首行」，不倒整串 repr。"""
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA, "Referer": referer, "Accept": "*/*",
        })
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
        ms = int((time.time() - started) * 1000)
        head = body[:70].decode("utf-8", "replace").replace("\n", " ")
        return f"OK   HTTP {resp.status} {len(body):>6}B {ms:>5}ms  {head}"
    except Exception as exc:                              # noqa: BLE001
        ms = int((time.time() - started) * 1000)
        detail = (str(exc).strip().splitlines() or [""])[0][:70]
        return f"FAIL {type(exc).__name__}: {detail}  ({ms}ms)"


def main() -> None:
    parser = argparse.ArgumentParser(description="东财行情链路连通性探针（只读）")
    parser.add_argument("--timeout", type=int, default=8, help="单次请求超时秒数（默认 8）")
    args = parser.parse_args()

    print("===== 1) DNS 解析 =====")
    for host in _PROBE_PATHS:
        try:
            ips = sorted({ai[4][0] for ai in
                          socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
            print(f"  {host:30s} -> {', '.join(ips[:3])}")
        except Exception as exc:                          # noqa: BLE001
            print(f"  {host:30s} -> 解析失败 {type(exc).__name__}")

    for scheme in ("https", "http"):
        print(f"\n===== 2) {scheme.upper()} 请求 =====")
        for host, path in _PROBE_PATHS.items():
            referer = ("https://finance.sina.com.cn" if "sina" in host
                       else "https://quote.eastmoney.com/")
            print(f"  {host:30s} {probe(f'{scheme}://{host}{path}', referer, args.timeout)}")

    print("\n===== 3) 结论怎么看 =====")
    print("  push2* 全 FAIL、futsseapi OK -> 实时行情自动走降级通道，昨收/增仓为空（显示 --）")
    print("  futsseapi 也 FAIL           -> 东财按出口 IP 段整体屏蔽，需换数据源或走代理")
    print("  HTTP 通而 HTTPS 不通        -> 本机到该 CDN 节点的 TLS/网络问题")
    print("  新浪 403/456                -> 该出口 IP 被新浪拉黑（机房 IP 常见）")


if __name__ == "__main__":
    main()
