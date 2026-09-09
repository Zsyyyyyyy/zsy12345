"""国内期货交易规则数据适配层。

通过 AkShare 读取九期网公开的合约级交易规则。这里统一完成字段名、
百分比和时间格式转换，业务层不直接依赖 DataFrame 的中文列名。
"""
from datetime import datetime
import re


EXCHANGE_NAMES = (
    "上海期货交易所",
    "上海国际能源交易中心",
    "大连商品交易所",
    "郑州商品交易所",
    "中国金融期货交易所",
    "广州期货交易所",
)


def _normalize_contract_symbol(symbol: str, exchange_name: str, now: datetime | None = None) -> str:
    """将郑商所三位交割月代码补成项目使用的四位年份代码。"""
    if exchange_name != "郑州商品交易所":
        return symbol
    match = re.fullmatch(r"([A-Z]+)(\d)(\d{2})", symbol)
    if match is None:
        return symbol
    current_year = (now or datetime.now()).year
    year = current_year - current_year % 10 + int(match.group(2))
    if year < current_year - 1:
        year += 10
    return f"{match.group(1)}{year % 100:02d}{match.group(3)}"


def _positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    try:
        # pandas Timestamp 和 datetime 都支持 to_pydatetime/datetime 接口。
        return value.to_pydatetime() if hasattr(value, "to_pydatetime") else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def get_contract_margin_rates() -> dict[str, dict]:
    """返回 ``SYMBOL -> {rate, updated_at, source}``。

    买开、卖开比例不同时取较低值，符合“交易所最低保证金比例”的展示语义。
    外部数据以百分数返回（7 表示 7%），这里统一转换成 0.07。
    """
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("缺少 akshare，无法刷新交易所保证金比例") from exc

    result: dict[str, dict] = {}
    errors: list[str] = []
    for exchange_name in EXCHANGE_NAMES:
        try:
            frame = ak.futures_comm_info(symbol=exchange_name)
        except Exception as exc:
            errors.append(f"{exchange_name}: {exc}")
            continue
        for row in frame.to_dict(orient="records"):
            symbol = str(row.get("合约代码") or "").strip().upper()
            if not symbol or not any(char.isdigit() for char in symbol):
                continue
            symbol = _normalize_contract_symbol(symbol, exchange_name)
            sides = [
                value for value in (
                    _positive_number(row.get("保证金-买开")),
                    _positive_number(row.get("保证金-卖开")),
                )
                if value is not None
            ]
            if not sides:
                continue
            result[symbol] = {
                "rate": min(sides) / 100,
                "updated_at": _parse_datetime(row.get("手续费更新时间")),
                "source": "AKShare/九期网",
            }
    if not result:
        detail = "；".join(errors) if errors else "外部接口未返回数据"
        raise RuntimeError("保证金规则刷新失败：" + detail)
    return result
