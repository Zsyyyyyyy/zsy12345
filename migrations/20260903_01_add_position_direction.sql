-- 迁移 20260903_01：持仓/结算表加 direction 列，放开 positions 同品种唯一约束
--
-- 执行：./venv/bin/python migrate.py up
-- 回滚：./venv/bin/python migrate.py down 20260903_01_add_position_direction
--
-- INSTANT 说明：
--   MySQL 8.0.12+ 支持 INSTANT 加列（只改元数据，毫秒级，不锁表、不重建表）。
--   显式写 ALGORITHM=INSTANT 是刻意的安全阀——一旦 MySQL 判定该操作不支持 INSTANT，
--   会直接报错中止，而不是偷偷退化成 COPY（全表重建 + 写锁）。
--   表数据量小时无所谓，数据量大时这条语句就是「无感」与「停机半小时」的分界线。

ALTER TABLE positions
  ADD COLUMN direction VARCHAR(8) NOT NULL DEFAULT 'long'
  COMMENT '方向：long=做多 / short=做空' AFTER code,
  ALGORITHM=INSTANT;

ALTER TABLE settlements
  ADD COLUMN direction VARCHAR(8) NOT NULL DEFAULT 'long'
  COMMENT '开仓方向快照：long=做多 / short=做空' AFTER code,
  ALGORITHM=INSTANT;

-- 去掉品种唯一约束：允许同一品种建多条持仓（分批建仓等场景）。
-- 删二级索引用 INPLACE + LOCK=NONE：不重建表，期间仍可读写。
ALTER TABLE positions
  DROP INDEX uq_positions_user_code,
  ALGORITHM=INPLACE, LOCK=NONE;
