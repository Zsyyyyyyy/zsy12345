# 期货行情看板 · 说明

行情看板已整合进 FastAPI 主后端，不再是独立的 `web-futures` 服务。

- 前端：`modules/futures/public/dashboard.html`
- 入口路由：`GET /futures`（见 `app/main.py`）
- 数据源：新浪行情接口（服务端代理，补 Referer + GB18030→UTF-8，返回结构化 JSON）

## 相关接口

| 接口 | 说明 |
|---|---|
| `GET /api/futures?codes=nf_RB2701,sh600519,sh000300` | 实时行情（A股 + 国内期货） |
| `GET /api/futures/suggest?key=铜` | 行情搜索联想（A股 + 国内期货） |
| `GET /api/futures/minline?symbol=RB2701` | 国内期货分时 |
| `GET /api/futures/dailykline?symbol=RB2701` | 国内期货日 K |
| `GET /api/positions` | 我的持仓 CRUD |
| `GET /api/positions/{id}/settle` / `GET /api/settlements` | 结算 |
| `GET /api/groups` | 看盘分组 |
| `GET /api/futures-base` | 合约库（默认只看可交易） |
| `GET /api/futures-base/search?key=螺纹钢` | 合约关键字搜索（新增持仓联想） |
| `GET /api/futures/hist-position` | 历史价格位置 |
| `GET /api/history/dailybars` | 历史日K（读库） |

## 合约库 futures_base

`futures_base` 表存的是**真实挂牌合约**（如 `nf_RB2701`，含到期历史合约），由
`refresh_tradable_futures.py` 定时从新浪刷新（只增不删、无 is_active）：

```bash
venv/bin/python refresh_tradable_futures.py            # 拉取 + upsert
venv/bin/python refresh_tradable_futures.py --dry-run  # 只看不写
```

「当前可交易」不再靠状态位，统一按 **symbol 交割年月 >= 当前月** 判断：
「新增持仓」的搜索下拉与提交校验都只认未到期的合约（如 2027-02 时 RB2701 到期，
01 合约会轮到 RB2801）。
