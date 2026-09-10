"""期货合约目录的抓取与刷新服务。"""
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.akshare_client import fetch_contracts, fetch_varieties
from app.clients.exchange_rules_client import get_contract_margin_rates
from app.fetchutils import MULTIPLIERS, SLEEP, _CONTRACT_RE
from app.models import FuturesBase

_MAX_FAIL_STREAK = 5   # 连续这么多品种拉取失败就提前中止，避免全网不通时空跑


def fetch_nodes() -> list[tuple[str, str, str]]:
    """拉取品种映射，返回 (中文品种名, akshare 品种名, 交易所代码)。

    数据源是 akshare 的 futures_symbol_mark()（内部即新浪品种表），
    不再手工解析 qihuohangqing.js 文本。
    """
    return [(v["name"], v["ak_symbol"], v["exchange"]) for v in fetch_varieties()]


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


def refresh_contracts(db: Session, dry_run: bool = False, log=None) -> dict:
    """抓取当前挂牌合约并幂等写入 futures_base，只增不删。"""
    def say(message: str) -> None:
        if log is not None:
            log(message)

    nodes = fetch_nodes()
    total_nodes = len(nodes)
    say(f"品种数：{total_nodes}")
    inserted = updated = unchanged = skipped = failed = 0
    streak = 0  # 连续失败计数：数据源整体不可达时没必要把 86 个品种全跑一遍

    for index, (underlying_name, ak_symbol, exchange) in enumerate(nodes, 1):
        try:
            contracts = fetch_contracts(ak_symbol)
        except Exception as exc:
            failed += 1
            streak += 1
            say(f"  ✗ [{index}/{total_nodes}] {underlying_name} 拉取失败：{exc}")
            if streak >= _MAX_FAIL_STREAK:
                say(f"连续 {streak} 个品种失败，判定数据源不可达，提前中止（已写入部分不回滚）")
                break
            continue
        streak = 0
        say(f"  ✓ [{index}/{total_nodes}] {underlying_name:<8} 合约 {len(contracts)} 个")

        for contract in contracts:
            symbol = (contract.get("symbol") or "").upper()
            match = _CONTRACT_RE.match(symbol)
            if not match:
                skipped += 1
                continue
            underlying = match.group(1)
            name = contract.get("name") or ""
            if name.endswith("连续"):
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
        time.sleep(SLEEP)

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
        f"跳过(连续等) {skipped} / 拉取失败品种 {failed}")
    if not dry_run:
        say(f"✅ 完成：合约库共 {total} 条（是否可交易由交割月份判断，本脚本不再维护）")
    else:
        say("[DRY-RUN] 未提交")
    return {
        "dry_run": dry_run, "nodes": len(nodes), "inserted": inserted,
        "updated": updated, "unchanged": unchanged, "skipped": skipped,
        "failed": failed, "total": total,
        "margins": margin_result,
    }


def refresh_margin_rates(db: Session, dry_run: bool = False, log=None) -> dict:
    """刷新当前挂牌合约的最低交易所保证金比例。

    外部规则抓取失败时保留库内旧值，不能用空值覆盖最后一次有效数据。
    """
    try:
        rules = get_contract_margin_rates()
    except Exception as exc:
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
