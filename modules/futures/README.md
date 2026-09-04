# 期货行情看板 · 说明

行情看板已整合进 FastAPI 主后端，不再是独立的 `web-futures` 服务。

- 前端：`modules/futures/public/dashboard.html`
- 入口路由：`GET /futures`（见 `app/main.py`）
- 数据源：新浪行情接口（服务端代理，补 Referer + GB18030→UTF-8，返回结构化 JSON）

## 相关接口

| 接口 | 说明 |
|---|---|
| `GET /api/futures?codes=nf_RB2701,hf_OIL,sh600519` | 实时行情 |
| `GET /api/futures/suggest?key=铜` | 行情搜索联想 |
| `GET /api/futures/minline?symbol=RB2701` | 国内期货分时 |
| `GET /api/futures/dailykline?symbol=RB2701` | 国内期货日 K |
| `GET /api/positions` | 我的持仓 CRUD |
| `GET /api/positions/{id}/settle` / `GET /api/settlements` | 结算 |
| `GET /api/groups` | 看盘分组 |
| `GET /api/tradable-futures` | 可交易合约字典 |
| `GET /api/tradable-futures/search?key=螺纹钢` | 合约关键字搜索 |

## 可交易合约字典

`tradable_futures` 表存的是**真实挂牌合约**（如 `nf_RB2701`），每天 9:00 由定时任务从新浪自动刷新：

```bash
venv/bin/python refresh_tradable_futures.py            # 拉取 + upsert
venv/bin/python refresh_tradable_futures.py --dry-run  # 只看不写
```

「新增持仓」的搜索下拉只显示表中 `is_active=True` 的合约。
