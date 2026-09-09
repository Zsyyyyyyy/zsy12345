"""国内期货实时行情（纯 akshare）。

只服务 nf_ 开头的国内期货代码，股票/指数不再处理。

数据源优先级（各自带 sticky 开关，失败一次后不再空跑）：
  1. ak.futures_zh_realtime —— 新浪 vip.stock.finance.sina.com.cn 的 JSON 接口
     （与被机房封禁的 hq.sinajs.cn 不是同一个域；合约目录 refresh_contracts
      ���的就是它）。按品种聚合请求，主力 nf_XX0 = 该品种持仓量最大的合约。
  2. ak.futures_zh_spot —— 新浪 hq.sinajs.cn（机房 IP 必被 403 Forbidden，
     仅本地/家宽可用）

运行环境三点必须注意（踩过的坑）：
  - **新浪全站防盗链**：不带 Referer 一律 403 Forbidden（本地家宽和机房都一样，
    跟 IP 无关）。akshare 内部是裸 requests.get(url)，没机会带 Referer，
    所以模块加载时包一层 Session.request 全局注入。这是部署到机房后
    /api/futures 502 的真正原因——不是 IP 被封，别再往换数据源的方向查。
  - 环境/系统代理会让 requests 全线 ProxyError，故强制 no_proxy=*
  - 东财/新浪会 RST python-requests 默认 UA，故全进程 UA 换成 Chrome

返回结构：{code, name, open, high, low, price, yestclose, volume, time}
"""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# 代理：行情源全是国内站点，走代理只会 ProxyError
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

# UA：被 RST 的都是 python-requests 裸 UA 的请求
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 新浪防盗链：不带 Referer 直接 403 Forbidden（服务器上必现，本地家宽同样 403）。
# akshare 内部是裸 requests.get，没机会带 Referer，只能在这里全进程注入。
_SINA_REFERER = "https://finance.sina.com.cn/"

try:
    import requests.sessions as _sessions
    import requests.utils

    # default_user_agent 在 utils 里是运行时按名字解析的，改它能生效；
    # 但 sessions.py 在 import 时已绑定 default_headers 函数对象，改 utils.default_headers 无效，
    # 所以 Referer 只能靠包一层 Session.request。
    requests.utils.default_user_agent = lambda: _CHROME_UA
    _orig_session_request = _sessions.Session.request

    def _session_request(self, method, url, *args, **kwargs):
        headers = kwargs.get("headers")
        if headers is None:
            headers = {}          # akshare 全是裸 requests.get(url)，不会传 headers
        if isinstance(headers, dict):
            headers = dict(headers)
            headers.setdefault("Referer", _SINA_REFERER)
            headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
            kwargs["headers"] = headers
        return _orig_session_request(self, method, url, *args, **kwargs)

    _sessions.Session.request = _session_request
except Exception as e:  # noqa: BLE001
    logger.warning("requests 全局补丁失败: %s", e)

_vip_sina_unavailable = False   # ak.futures_zh_realtime（vip 新浪域）是否已被封
_hq_sina_unavailable = False    # ak.futures_zh_spot（hq 新浪域）是否已被封
_CFFEX_RE = re.compile(r"^nf_(IF|IH|IC|IM|TF|TS|T\d|TL)")  # 中金所品种
_LAST_ERRORS: list[str] = []    # 本次请求的失败原因链，502 时回传给前端便于定位


def _note_error(msg: str) -> None:
    """记录失败原因：既进日志（服务器 uvicorn 能看到），也进 502 的 detail。"""
    logger.error("行情失败原因: %s", msg)
    _LAST_ERRORS.append(msg)


def _s(v) -> str:
    """pandas 值 -> 干净字符串；None/NaN/'-'/占位符统一成空串。"""
    if v is None:
        return ""
    try:
        if v != v:  # NaN
            return ""
    except TypeError:
        pass
    s = str(v).strip()
    return "" if s in ("", "-", "nan", "None") else s


def _retry(fn, times: int = 3, delay: float = 0.5):
    """CDN 偶发 RST（RemoteDisconnected），间隔重试基本能过。"""
    for i in range(times):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == times - 1:
                raise
            logger.warning("akshare 请求被断开，%.1fs 后重试(%d/%d): %s",
                           delay * (i + 1), i + 1, times - 1, e)
            time.sleep(delay * (i + 1))


# 品种代码 -> 新浪品种 node（vip.stock.finance.sina.com.cn 的 node 参数值是拼音码，
# 不是"品种+0"！如 RB 对应 lwg_qh、AU 对应 hj_qh、MA 对应 zc_qh）。
# 本表 2026-09-09 遍历新浪全部 node 探测生成（86 个品种），新品种上市时补一行即可。
_VARIETY_NODE: dict[str, str] = {
    "A": "dd_qh", "AD": "ad_qh", "AG": "by_qh", "AL": "lv_qh", "AO": "ao_qh", "AP": "xpg_qh",
    "AU": "hj_qh", "B": "de_qh", "BB": "jhb_qh", "BC": "bc_qh", "BR": "br_qh", "BU": "lq_qh",
    "BZ": "bz_qh", "C": "hym_qh", "CF": "mh_qh", "CJ": "hz_qh", "CS": "ymdf_qh", "CU": "tong_qh",
    "CY": "ms_qh", "EB": "byx_qh", "EC": "ec_qh", "EG": "yec_qh", "FB": "xwb_qh", "FG": "bl_qh",
    "FU": "ry_qh", "HC": "rzjb_qh", "I": "tks_qh", "IC": "zzgz_qh", "IF": "qz_qh", "IH": "szgz_qh",
    "IM": "im_qh", "J": "jt_qh", "JD": "jd_qh", "JM": "jm_qh", "JR": "jdm_qh", "L": "lldpe_qh",
    "LC": "lc_qh", "LG": "lg_qh", "LH": "lh_qh", "LR": "wxd_qh", "LU": "lu_qh", "M": "dp_qh",
    "MA": "zc_qh", "NI": "ni_qh", "NR": "ehj_qh", "OI": "czy_qh", "OP": "op_qh", "P": "zly_qh",
    "PB": "qian_qh", "PD": "pd_qh", "PF": "pf_qh", "PG": "pg_qh", "PK": "pk_qh", "PL": "pl_qh",
    "PP": "jbx_qh", "PR": "pr_qh", "PS": "ps_qh", "PT": "pt_qh", "PX": "px_qh", "RB": "lwg_qh",
    "RI": "zxd_qh", "RM": "czp_qh", "RR": "gm_qh", "RS": "ycz_qh", "RU": "xj_qh", "SA": "cj_qh",
    "SC": "yy_qh", "SF": "gt_qh", "SH": "sh_qh", "SI": "si_qh", "SM": "mg_qh", "SN": "xi_qh",
    "SP": "zj_qh", "SR": "bst_qh", "SS": "bxg_qh", "T": "sngz_qh", "TA": "pta_qh", "TF": "gz_qh",
    "TS": "engz_qh", "UR": "ns_qh", "V": "pvc_qh", "WH": "qm_qh", "WR": "xc_qh", "Y": "dy_qh",
    "ZC": "dlm_qh", "ZN": "xing_qh",
}


@lru_cache(maxsize=1)
def _fut_node_symbol_map() -> dict[str, str]:
    """node -> akshare 品种名（akshare 只认品种名，内部再转成 node）。"""
    import akshare as ak
    df = ak.futures_symbol_mark()
    return dict(zip(df["mark"].astype(str).str.lower(), df["symbol"].astype(str)))


# ---------------------------------------------------------------- 数据源 1：vip 新浪域

def _ak_futures_realtime(fut_codes: list[str]) -> tuple[list[dict], list[str]]:
    """按品种并发请求 ak.futures_zh_realtime；返回 (items, 未取到的 codes)。"""
    import akshare as ak

    try:
        node_sym_map = _fut_node_symbol_map()
    except Exception as e:  # noqa: BLE001
        _note_error(f"ak.futures_symbol_mark 失败(会导致全部期货取不到): {e!r}")
        return [], fut_codes

    groups: dict[str, list[str]] = {}
    unknown: list[str] = []
    for c in fut_codes:
        m = re.match(r"^nf_([A-Za-z]+)", c)  # 不强制大写：畸形/小写代码不应让整个请求 500
        if not m:
            unknown.append(c)
            continue
        prod = m.group(1).upper()
        groups.setdefault(prod, []).append(c)

    def fetch_group(item: tuple[str, list[str]]) -> tuple[list[dict], list[str]]:
        prod, codes = item
        node = _VARIETY_NODE.get(prod)
        sym = node_sym_map.get(node.lower()) if node else None
        if not node or not sym:
            _note_error(f"品种 {prod} 无新浪 node 映射或 akshare 未收录: node={node}")
            return [], codes
        try:
            df = _retry(lambda: ak.futures_zh_realtime(symbol=sym))
        except Exception as e:  # noqa: BLE001
            _note_error(f"ak.futures_zh_realtime(品种={prod}, node={node}) 失败: {e!r}")
            return [], codes
        if df is None or df.empty:
            _note_error(f"ak.futures_zh_realtime(品种={prod}, node={node}) 返回空表")
            return [], codes
        rows = {str(r["symbol"]).upper(): r for _, r in df.iterrows()}
        main_idx = None
        try:
            main_idx = df["position"].idxmax()  # 主力 = 持仓量最大
        except Exception:  # noqa: BLE001
            pass
        items: list[dict] = []
        missed: list[str] = []
        for c in codes:
            target = c.removeprefix("nf_").upper()
            r = rows.get(target)
            if r is None and main_idx is not None:
                # 未上市/已下市的合约（含 nf_XX0 主力连续）统一回落到主力合约，避免整块空白
                r = df.loc[main_idx]
                logger.warning("合约 %s 不在 %s 合约表中，回落到主力 %s",
                               c, prod, _s(r.get("symbol")))
            if r is None:
                missed.append(c)
                continue
            items.append({
                "code": c,
                "source_symbol": _s(r.get("symbol")),
                "name": _s(r.get("name")) or _s(r.get("symbol")) or c,
                "open": _s(r.get("open")),
                "high": _s(r.get("high")),
                "low": _s(r.get("low")),
                "price": _s(r.get("trade")),
                "yestclose": _s(r.get("presettlement") or r.get("prevsettlement") or r.get("preclose")),
                "volume": _s(r.get("volume")),
                "time": _s(r.get("time") or r.get("datetime")),
            })
        return items, missed

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(fetch_group, groups.items()))
    return ([it for items, _ in pairs for it in items],
            unknown + [c for _, missed in pairs for c in missed])


# ---------------------------------------------------------------- 数据源 2：hq 新浪域

def _ak_futures_spot(fut_codes: list[str]) -> tuple[list[dict], list[str]]:
    """逐合约并发请求 ak.futures_zh_spot；返回 (items, 未取到的 codes)。"""

    def fetch(code: str) -> dict | None:
        try:
            import akshare as ak
            symbol = code.removeprefix("nf_")
            market = "CFFEX" if _CFFEX_RE.match(code) else "CF"
            df = _retry(lambda: ak.futures_zh_spot(symbol=symbol, market=market, adjust="0"))
            if df is None or df.empty:
                return None
            row = df.iloc[0]
            # CF 分支 last_settle_price=昨结算；CFFEX 分支无昨结字段
            yestclose = row.get("last_settle_price") if market == "CF" else None
            return {
                "code": code,
                "name": _s(row.get("symbol")) or code,
                "open": _s(row.get("open")),
                "high": _s(row.get("high")),
                "low": _s(row.get("low")),
                "price": _s(row.get("current_price")),
                "yestclose": _s(yestclose),
                "volume": _s(row.get("volume")),
                "time": _s(row.get("time")),
            }
        except Exception as e:  # noqa: BLE001
            _note_error(f"ak.futures_zh_spot({code}) 失败: {e!r}")
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(fetch, fut_codes))
    return ([g for g in got if g],
            [c for c, g in zip(fut_codes, got) if g is None])


# ---------------------------------------------------------------- 统一入口

def get_quotes(codes: list[str]) -> list[dict]:
    """国内期货实时行情入口（只处理 nf_ 代码）。返回顺序跟随入参。

    任何未预期异常都转成 502 + traceback 日志，绝不让接口抛 500。
    """
    try:
        return _get_quotes(codes)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("期货行情获取异常: %s", e)
        raise HTTPException(status_code=502, detail=f"期货行情获取异常: {e}")


def _get_quotes(codes: list[str]) -> list[dict]:
    global _vip_sina_unavailable, _hq_sina_unavailable

    _LAST_ERRORS.clear()
    clean = [str(c).strip() for c in codes if str(c).strip()]
    fut_codes = [c for c in clean if c.startswith("nf_")]
    result: dict[str, dict] = {}
    pending = fut_codes

    # 阶梯 1：vip 新浪域（服务器可用）
    if pending and not _vip_sina_unavailable:
        try:
            items, pending = _ak_futures_realtime(pending)
            for it in items:
                result[it["code"]] = it
            if not items and pending == fut_codes:
                _vip_sina_unavailable = True
        except Exception as e:  # noqa: BLE001
            _vip_sina_unavailable = True
            logger.exception("futures_zh_realtime 整体不可用: %s", e)

    # 阶梯 2：hq 新浪域（本地/家宽可用）
    if pending and not _hq_sina_unavailable:
        try:
            items, pending = _ak_futures_spot(pending)
            for it in items:
                result[it["code"]] = it
            if not items and pending == fut_codes:
                _hq_sina_unavailable = True
        except Exception as e:  # noqa: BLE001
            _hq_sina_unavailable = True
            logger.exception("futures_zh_spot 整体不可用: %s", e)

    if pending:
        logger.warning("以下期货代码两个 akshare 数据源均未取到: %s", pending)
    if not result and clean:
        reason = "；".join(_LAST_ERRORS[-5:]) or "未知原因（日志无异常，多为数据源返回空）"
        raise HTTPException(status_code=502,
                            detail=f"akshare 未取到任何期货行情 [{codes}]：{reason}")
    return [result[c] for c in clean if c in result]


# ---------------------------------------------------------------- 自诊断

def diagnose(codes: list[str]) -> dict:
    """逐层探测行情链路，定位服务器 502 到底断在哪一环。/api/futures/diag 用。"""
    import platform
    import sys

    report: dict = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "proxy_env": {k: os.environ.get(k) for k in
                      ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "no_proxy", "NO_PROXY")},
        "codes": codes,
    }

    # 1) akshare 是否装得上
    try:
        import akshare as ak
        report["akshare"] = {"imported": True, "version": getattr(ak, "__version__", "?")}
    except Exception as e:  # noqa: BLE001
        report["akshare"] = {"imported": False, "error": f"{e!r}"}
        report["结论"] = "akshare 未安装/导入失败：服务器上执行 pip install -U akshare"
        return report

    # 2) 品种表（futures_symbol_mark）
    try:
        df = ak.futures_symbol_mark()
        node_sym = dict(zip(df["mark"].astype(str).str.lower(), df["symbol"].astype(str)))
        report["symbol_mark"] = {"ok": True, "count": len(node_sym)}
    except Exception as e:  # noqa: BLE001
        report["symbol_mark"] = {"ok": False, "error": f"{e!r}"}
        report["结论"] = ("取不到品种表 vip.stock.finance.sina.com.cn/.../qihuohangqing.js"
                          "——服务器到新浪 vip 域不通")
        return report

    # 3) 每个 code 的 node 解析 + 实时行情
    per: list[dict] = []
    for c in codes:
        m = re.match(r"^nf_([A-Za-z]+)", str(c).strip())
        item: dict = {"code": c}
        if not m:
            item["error"] = "不是 nf_ 开头的期货代码"
            per.append(item)
            continue
        prod = m.group(1).upper()
        node = _node_of(prod)
        item["product"] = prod
        item["node"] = node
        item["ak_symbol"] = node_sym.get((node or "").lower())
        if not item["ak_symbol"]:
            item["error"] = "无 node 映射或 akshare 未收录该品种"
            per.append(item)
            continue
        try:
            d = ak.futures_zh_realtime(symbol=item["ak_symbol"])
            item["realtime"] = {"ok": True, "rows": 0 if d is None else len(d),
                                "symbols": [] if d is None or d.empty
                                else [str(x) for x in d["symbol"].head(20)]}
            hit = None
            if d is not None and not d.empty:
                tgt = c.removeprefix("nf_").upper()
                for _, r in d.iterrows():
                    if str(r["symbol"]).upper() == tgt:
                        hit = r
                        break
            item["hit"] = None if hit is None else {
                "symbol": _s(hit.get("symbol")), "name": _s(hit.get("name")),
                "price": _s(hit.get("trade")), "time": _s(hit.get("time"))}
        except Exception as e:  # noqa: BLE001
            item["realtime"] = {"ok": False, "error": f"{e!r}"}
        # 4) hq.sinajs 兜底通道
        try:
            market = "CFFEX" if _CFFEX_RE.match(c) else "CF"
            s = ak.futures_zh_spot(symbol=c.removeprefix("nf_"), market=market, adjust="0")
            item["spot"] = {"ok": True, "price": _s(s.iloc[0].get("current_price"))} \
                if s is not None and not s.empty else {"ok": False, "error": "空表"}
        except Exception as e:  # noqa: BLE001
            item["spot"] = {"ok": False, "error": f"{e!r}"}
        per.append(item)
    report["per_code"] = per

    ok = any(i.get("hit") or i.get("spot", {}).get("ok") for i in per)
    report["结论"] = ("行情链路正常" if ok else
                     "所有数据源都失败：看 per_code 里的 error 字段定位（多为 403 Forbidden → 缺 Referer）")
    return report


def _node_of(prod: str) -> str | None:
    """诊断用：拿 node，不抛异常。"""
    return _VARIETY_NODE.get(prod.upper())
