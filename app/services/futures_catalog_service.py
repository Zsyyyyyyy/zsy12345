"""期货合约目录的抓取与刷新服务（东方财富）。

品种范围来自本地码表 config/futures_varieties.json（只刷新这里列出的品种）；
文件不存在才回退东财品种表（全部品种）。

与改造前的差别：东财的合约目录接口是按「交易所」一次返回该市场全部挂牌合约，
所以一轮刷新只需 5~6 次请求（原来按新浪 node 逐个品种请求，86 个品种就是 86 次）。
"""
import json
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.eastmoney_client import (
    DB_EXCHANGE_OF_MARKET,
    MARKET_IDS,
    MARKET_NAMES,
    UpstreamBlocked,
    fetch_market_contracts,
    fetch_varieties,
    market_of_variety,
)
from app.clients.exchange_rules_client import get_contract_margin_rates
from app.fetchutils import MULTIPLIERS, _CONTRACT_RE
from app.models import FuturesBase

# 东财按交易所取数，市场数量很少，间隔只为礼貌性限速
_MARKET_SLEEP = float(os.getenv("FUTURES_REFRESH_SLEEP", "0.3"))

# 本地品种码表：只刷新这里列出的品种。
# 优先于东财品种表——后者无法限定品种范围（会把普麦、粳稻等冷门品种一起刷进来）。
VARIETIES_FILE = Path(os.getenv(
    "FUTURES_VARIETIES_FILE",
    str(Path(__file__).resolve().parents[2] / "config" / "futures_varieties.json"),
))


def load_varieties_config() -> dict[str, dict[str, str]] | None:
    """读取 config/futures_varieties.json -> {交易所: {品种代码: 中文名}}。

    文件不存在返回 None（由调用方回退东财品种表）。
    结构：{"CZCE": {"SA": "纯碱", ...}, "SHFE": {"RB": "螺纹钢", ...}}
    值也兼容旧写法 {"name": "纯碱", "node": "..."}（只取 name）。
    下划线开头的键（_readme/_updated）当注释跳过。
    """
    if not VARIETIES_FILE.exists():
        return None
    try:
        data = json.loads(VARIETIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"品种码表解析失败 {VARIETIES_FILE}：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"品种码表格式错误（应为对象）: {VARIETIES_FILE}")

    out: dict[str, dict[str, str]] = {}
    for exchange, block in data.items():
        if exchange.startswith("_") or not isinstance(block, dict):
            continue
        items: dict[str, str] = {}
        for code, meta in block.items():
            name = meta.get("name") if isinstance(meta, dict) else meta
            name = str(name or "").strip()
            if name:
                items[code.strip().upper()] = name
        if items:
            out[exchange.strip().upper()] = items
    if not out:
        raise RuntimeError(f"品种码表为空: {VARIETIES_FILE}")
    return out


def load_wanted_varieties() -> tuple[dict[str, tuple[str, str]], str]:
    """要刷新的品种：{品种代码: (中文名, 交易所)}，以及来源（config / remote）。"""
    config = load_varieties_config()
    if config is not None:
        wanted = {code: (name, exchange)
                  for exchange, items in config.items()
                  for code, name in items.items()}
        return wanted, "config"
    return ({v["variety"]: (v["name"], v["exchange"]) for v in fetch_varieties()}, "remote")


def _upsert_contract(
    db: Session,
    code: str,
    symbol: str,
    name: str,
    underlying: str,
    underlying_name: str,
    exchange: str,
    multiplier: float | None,
    tick_size: float | None,
    dry_run: bool,
) -> str:
    existing = db.scalar(select(FuturesBase).where(FuturesBase.code == code))
    if existing is None:
        if not dry_run:
            db.add(FuturesBase(
                code=code, symbol=symbol, name=name, underlying=underlying,
                underlying_name=underlying_name, exchange=exchange,
                multiplier=multiplier, tick_size=tick_size,
            ))
        return "inserted"

    changed = False
    for attr, value in (
        ("symbol", symbol), ("name", name), ("underlying", underlying),
        ("underlying_name", underlying_name), ("exchange", exchange),
        ("multiplier", multiplier), ("tick_size", tick_size),
    ):
        if getattr(existing, attr) != value:
            if not dry_run:
                setattr(existing, attr, value)
            changed = True
    return "updated" if changed else "unchanged"


def refresh_contracts(db: Session, dry_run: bool = False, log=None,
                      underlyings: list[str] | None = None) -> dict:
    """抓取当前挂牌合约并幂等写入 futures_base，只增不删。

    underlyings：临时只刷这些品种（如 ["RB", "SA"]），不修改码表文件；
                 None 表示用码表（或东财品种表）里的全部品种。
    """
    def say(message: str) -> None:
        if log is not None:
            log(message)

    wanted, source = load_wanted_varieties()
    if underlyings:
        keep = {u.strip().upper() for u in underlyings if u and u.strip()}
        wanted = {k: v for k, v in wanted.items() if k in keep}
        say(f"按 --underlyings 过滤：{'/'.join(sorted(keep))} → 命中 {len(wanted)} 个品种")

    label = {"config": f"码表 {VARIETIES_FILE}",
             "remote": "东财品种表"}.get(source, source)

    # 品种 -> 市场码；码表里没收录的品种会拿不到 market，单独统计并告警
    market_of: dict[str, int] = {}
    unmapped: list[str] = []
    for variety in wanted:
        market = market_of_variety(variety)
        if market is None:
            unmapped.append(variety)
        else:
            market_of[variety] = market

    markets = sorted(set(market_of.values()))
    say(f"品种 {len(wanted)} 个 / 交易所 {len(markets)} 个（来源：{label}）")
    if unmapped:
        say(f"⚠ 码表里这 {len(unmapped)} 个品种在东财没有对应市场，已跳过："
            f"{'/'.join(sorted(unmapped))}")

    inserted = updated = unchanged = skipped = failed = 0

    for index, market in enumerate(markets, 1):
        market_label = MARKET_NAMES.get(market, str(market))
        wanted_here = {v for v, m in market_of.items() if m == market}
        try:
            contracts = fetch_market_contracts(market)
        except UpstreamBlocked as exc:
            failed += 1
            say(f"  ✗ [{index}/{len(markets)}] {market_label} 被东财限流：{exc}")
            say("⚠ 已触发东财限流，本轮剩余交易所不再请求。"
                f"建议等 {int(os.getenv('FUTURES_EM_COOLDOWN', '600'))}s 后再试。")
            break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            say(f"  ✗ [{index}/{len(markets)}] {market_label} 拉取失败：{exc}")
            continue

        # 东财一次返回该市场全部合约，只保留码表里要的品种
        kept = [c for c in contracts if c["variety"] in wanted_here]
        say(f"  ✓ [{index}/{len(markets)}] {market_label:<6} 合约 {len(kept)} 个"
            f"（市场共 {len(contracts)} 个）")

        for contract in kept:
            symbol = (contract.get("symbol") or "").upper()
            match = _CONTRACT_RE.match(symbol)
            if not match:
                skipped += 1
                continue
            underlying = match.group(1)
            underlying_name, exchange = wanted[underlying]
            exchange = exchange or contract.get("exchange") or DB_EXCHANGE_OF_MARKET.get(market, "")
            # 名称统一成「品种中文名 + 4 位交割年月」，与库内既有数据一致
            # （东财郑商所合约名是 3 位月份，如「纯碱701」，需要改写）
            name = f"{underlying_name}{match.group(2)}"
            multiplier, tick_size = MULTIPLIERS.get(underlying, (None, None))
            action = _upsert_contract(
                db, "nf_" + symbol, symbol, name, underlying,
                underlying_name, exchange, multiplier, tick_size, dry_run,
            )
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1
            else:
                unchanged += 1

        if index < len(markets):
            time.sleep(_MARKET_SLEEP)

    # SessionLocal 使用 autoflush=False。空库重建时必须先把新增合约写入当前事务，
    # 否则下面的保证金查询看不到刚 db.add() 的合约，导致首轮保证金全部为空。
    if not dry_run:
        db.flush()
    margin_result = refresh_margin_rates(db, dry_run=dry_run, log=log)
    if not dry_run:
        db.commit()
    total = len(db.scalars(select(FuturesBase)).all())
    say("-" * 60)
    say(f"新增 {inserted} / 更新 {updated} / 未变 {unchanged} / "
        f"跳过(非4位年月) {skipped} / 拉取失败交易所 {failed}")
    if not dry_run:
        say(f"✅ 完成：合约库共 {total} 条（是否可交易由交割月份判断，本脚本不再维护）")
    else:
        say("[DRY-RUN] 未提交")
    return {
        "dry_run": dry_run, "varieties_source": source, "nodes": len(wanted),
        "markets": [MARKET_NAMES.get(m, str(m)) for m in markets],
        "underlyings": sorted(wanted), "unmapped": sorted(unmapped),
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "skipped": skipped, "failed": failed, "total": total,
        "margins": margin_result,
    }


def refresh_margin_rates(db: Session, dry_run: bool = False, log=None) -> dict:
    """刷新当前挂牌合约的最低交易所保证金比例。

    外部规则抓取失败时保留库内旧值，不能用空值覆盖最后一次有效数据。
    """
    try:
        rules = get_contract_margin_rates()
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log(f"⚠ 保证金比例刷新失败，保留原数据：{exc}")
        return {"updated": 0, "matched": 0, "failed": True, "detail": str(exc)}

    rows = db.scalars(select(FuturesBase).where(FuturesBase.symbol.in_(rules))).all()
    updated = 0
    for row in rows:
        rule = rules[row.symbol]
        changed = (
            row.exchange_margin_rate != rule["rate"]
            or row.margin_updated_at != rule["updated_at"]
            or row.margin_source != rule["source"]
        )
        if changed:
            updated += 1
            if not dry_run:
                row.exchange_margin_rate = rule["rate"]
                row.margin_updated_at = rule["updated_at"]
                row.margin_source = rule["source"]
    if log is not None:
        log(f"保证金规则 {len(rules)} 条 / 匹配合约 {len(rows)} 条 / 更新 {updated} 条")
    return {"updated": updated, "matched": len(rows), "received": len(rules), "failed": False}
