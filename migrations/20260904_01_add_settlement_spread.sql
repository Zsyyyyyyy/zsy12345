-- 迁移 20260904_01：结算表加 spread 列（按方向价差），支持部分结算
--
-- 执行：./venv/bin/python migrate.py up
-- 回滚：./venv/bin/python migrate.py down 20260904_01_add_settlement_spread
--
-- spread = 结算价 - 开仓价（做空方向取负），等价于 pnl / (手数 * 乘数)。
-- 单独存下来，结算历史里直接显示「按方向的价差」，不必每次现算。
--
-- 兼容性设计（这是能「无感」的关键）：
--   1. 列带 DEFAULT 0 且 NOT NULL —— 老代码 INSERT 时不写这一列也不会报错
--   2. 加列属于 Expand 阶段，新旧版本代码都能正常读写这张表
--   3. 因此可以先跑这条 DDL 再发新代码，中间没有不兼容窗口

ALTER TABLE settlements
  ADD COLUMN spread float NOT NULL DEFAULT 0
  COMMENT '按方向价差（做多=结算价-开仓价，做空=开仓价-结算价）' AFTER settle_price,
  ALGORITHM=INSTANT;
