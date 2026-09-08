"""国内期货合约代码解析与校验。

这些函数被 HTTP 接口和后台脚本共同使用，因此不能放在 routers 包里。
"""
import re
from datetime import date

from app.fetchutils import MULTIPLIERS, _CONTRACT_RE


_CODE4_RE = re.compile(r"^nf_([A-Za-z]+)(\d{4})$")


def parse_contract_code(code: str):
    """nf_RB2701 -> (underlying, year, month)，非法返回 None。"""
    raw = (code or "").strip().lower()
    match = _CODE4_RE.match(raw)
    if not match:
        return None
    underlying = match.group(1).upper()
    digits = match.group(2)
    year, month = 2000 + int(digits[:2]), int(digits[2:])
    if not 1 <= month <= 12:
        return None
    return underlying, year, month


def contract_month_of(symbol: str) -> int | None:
    match = _CONTRACT_RE.match((symbol or "").upper())
    if not match:
        return None
    month = int(match.group(2)[2:])
    return month if 1 <= month <= 12 else None


def is_live_symbol(symbol: str) -> bool:
    match = _CONTRACT_RE.match((symbol or "").upper())
    if not match:
        return True
    year, month = int(match.group(2)[:2]), int(match.group(2)[2:])
    if not 1 <= month <= 12:
        return True
    today = date.today()
    return date(2000 + year, month, 1) >= today.replace(day=1)


def validate_position_code(code: str) -> tuple[bool, str | None]:
    c = (code or "").strip()
    if c.startswith("nf_"):
        symbol = c[4:].upper()
        match = _CONTRACT_RE.match(symbol)
        if not match:
            return False, f"「{c}」不是有效合约：应形如 nf_RB2701（品种 + 4 位交割年月）"
        year, month = int(match.group(2)[:2]), int(match.group(2)[2:])
        if not 1 <= month <= 12:
            return False, f"「{c}」的月份不合法（应为 01~12，如 nf_RB2701）"
        today = date.today()
        if date(2000 + year, month, 1) < today.replace(day=1):
            return False, f"合约 {c} 已到期，交割月必须不早于当前月份"
        return True, None
    if re.fullmatch(r"[A-Za-z]{1,4}", c):
        return False, f"「{c}」只是品种名，请填写完整国内期货合约代码（如 RB2701）"
    if c.lower().startswith("hf_"):
        return False, "海外期货已下线：持仓/结算仅支持国内期货"
    if c.lower().startswith("hk") or re.match(r"^(sh|sz|bj)\d{6}$", c.lower()):
        return False, "股票/港股不支持记持仓：持仓/结算仅支持国内期货"
    return False, f"「{c}」不是有效的国内期货合约代码（应为 nf_ 开头）"


def auto_fill_multiplier(code: str) -> float | None:
    c = (code or "").strip()
    if not c.startswith("nf_"):
        return None
    match = _CONTRACT_RE.match(c[4:].upper())
    if not match:
        return None
    value = MULTIPLIERS.get(match.group(1))
    return value[0] if value else None
