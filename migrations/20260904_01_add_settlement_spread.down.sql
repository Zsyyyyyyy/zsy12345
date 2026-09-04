-- 回滚 20260904_01：删除 settlements.spread 列
--
-- 高危操作：执行前确认已无代码引用 spread 字段（例如 SettlementOut / 结算历史表格）。
-- 删除后该列数据不可恢复。

ALTER TABLE settlements
  DROP COLUMN spread,
  ALGORITHM=INSTANT;
