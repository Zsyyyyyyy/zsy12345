"""期货合约目录的抓取与刷新服务。"""
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.sina_client import get_contracts, get_node_list_text
from app.clients.exchange_rules_client import get_contract_margin_rates
from app.fetchutils import EXCHANGE_MAP, MULTIPLIERS, SLEEP, _CONTRACT_RE
from app.models import FuturesBase


def fetch_nodes() -> list[tuple[str, str, str]]:
    """拉取品种 node 映射，返回 (中文名, node, exchange)。"""
    source = get_node_list_text()
    result: list[tuple[str, str, str]] = []
    for exchange_key in ("czce", "dce", "shfe", "cffex", "gfex"):
        start = source.find(exchange_key + " :")
        if start < 0:
            start = source.find(exchange_key + ":")
        if start < 0:
            continue
        segment = source[start:]
        segment = segment[segment.find("["):]
        depth = 0
        end = None
        for index, char in enumerate(segment):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        array = segment[:end + 1] if end is not None else segment
        for name, node in re.findall(r"\['([^']+)',\s*'([^']+)'\s*,", array):
            if node.endswith("_qh"):
                result.append((name, node, EXCHANGE_MAP[exchange_key]))
    return result


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
    say(f"品种 node 数：{len(nodes)}")
    inserted = updated = unchanged = skipped = failed = 0

    for underlying_name, node, exchange in nodes:
        try:
            contracts = get_contracts(node)
        except Exception as exc:
            say(f"  ✗ {underlying_name:8} node={node:12} 拉取失败：{exc}")
            failed += 1
            continue

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
