-- 持仓 / 结算表迁移：加 direction 列（long=做多 / short=做空）+ 去掉 positions 同品种唯一约束
-- 本脚本涵盖今天所有数据库 schema 变更，幂等，可重复执行。
--
-- 什么时候需要跑：
--   库里已经存在 positions / settlements 表时才需要执行。
--   SQLAlchemy 的 create_all() 只会建新表，不会给已有表补列，
--   不跑这条的话接口会报 Unknown column 'positions.direction'。
--
--   如果是全新库、还没启动过服务，直接跳过本文件——建表时会自动带上 direction 列。
--
-- 执行：
--   mysql -u zsy -p123456 fastapi_auth < migrate_direction.sql
--
-- 若提示 Duplicate column name 'direction'，说明已经加过了，忽略即可。
--
-- 说明：已有持仓会全部落为 'long'（与改造前的默认多头行为一致），
--       之后在页面上把某条改成「做空」即可。

ALTER TABLE positions
  ADD COLUMN direction VARCHAR(8) NOT NULL DEFAULT 'long'
  COMMENT '方向：long=做多 / short=做空' AFTER code;

ALTER TABLE settlements
  ADD COLUMN direction VARCHAR(8) NOT NULL DEFAULT 'long'
  COMMENT '开仓方向快照：long=做多 / short=做空' AFTER code;

-- 去掉品种唯一约束：允许同一品种建多条持仓（分批建仓等场景）。
-- MySQL 8 没有 DROP INDEX IF EXISTS，若报 Unknown index 'uq_positions_user_code'
-- 说明约束早已不存在，忽略即可。
ALTER TABLE positions DROP INDEX uq_positions_user_code;
