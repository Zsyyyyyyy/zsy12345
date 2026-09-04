-- =====================================================================
-- create_tbl.sql —— FastAPI 期货看板 全量建表语句（MySQL 8.4）
--
-- 数据库：fastapi_auth
-- 字符集：utf8mb4 / utf8mb4_unicode_ci
-- 引擎：InnoDB
--
-- 对应 SQLAlchemy 模型：app/models/models.py
-- 只含 DDL，不含数据。按依赖顺序排列（均无外键，仅逻辑 user_id 关联）。
--
-- 说明：
--  * 表结构以 models.py 为准。历史库中 users.id 为 BIGINT UNSIGNED（手建遗留），
--    此处统一为 BIGINT（与 ORM BigInteger 一致），对自增主键无功能差异。
--  * schema_migrations 为手动迁移记录表（非 ORM 模型），用于留痕两次 ALTER：
--      20260903_01 给 positions 加 direction 列
--      20260904_01 给 settlements 加 spread 列
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. users 用户表
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `username`      VARCHAR(50)  NOT NULL COMMENT '用户名',
  `email`         VARCHAR(100) NOT NULL COMMENT '邮箱',
  `password_hash` VARCHAR(255) NOT NULL COMMENT '密码哈希（bcrypt）',
  `is_active`     TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否启用',
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`),
  UNIQUE KEY `uk_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ---------------------------------------------------------------------
-- 2. tradable_futures 国内可交易期货「真实合约」字典（全局共享）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `tradable_futures`;
CREATE TABLE `tradable_futures` (
  `code`            VARCHAR(20) NOT NULL COMMENT '完整合约代码（如 nf_RB2701），主键',
  `symbol`          VARCHAR(16) NOT NULL COMMENT '不带前缀合约代码（如 RB2701）',
  `name`            VARCHAR(50) NOT NULL COMMENT '合约中文名（如 螺纹钢2701）',
  `underlying`      VARCHAR(8)  NOT NULL COMMENT '品种代码（如 RB）',
  `underlying_name` VARCHAR(50) NOT NULL DEFAULT '' COMMENT '品种中文名（如 螺纹钢）',
  `exchange`        VARCHAR(8)  NOT NULL COMMENT '交易所 SHFE/DCE/CZCE/CFFEX/GFEX',
  `multiplier`      FLOAT       DEFAULT NULL COMMENT '合约乘数（每点价值，元；新品种可能为空）',
  `tick_size`       FLOAT       DEFAULT NULL COMMENT '最小变动价位',
  `is_active`       TINYINT(1)  NOT NULL DEFAULT 1 COMMENT '是否仍在交易（到期/下架置 0 保留历史）',
  `created_at`      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`code`),
  KEY `ix_tradable_futures_symbol` (`symbol`),
  KEY `ix_tradable_futures_underlying` (`underlying`),
  KEY `ix_tradable_futures_is_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='国内可交易期货真实合约字典';

-- ---------------------------------------------------------------------
-- 3. positions 期货/股票持仓（按用户隔离；同一品种可建多条）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `positions`;
CREATE TABLE `positions` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`    BIGINT      NOT NULL COMMENT '所属用户 id',
  `code`       VARCHAR(32) NOT NULL COMMENT '品种代码（如 nf_SA2701）',
  `direction`  VARCHAR(8)  NOT NULL DEFAULT 'long' COMMENT '方向：long=做多 / short=做空',
  `buy_price`  FLOAT       NOT NULL COMMENT '开仓价（做多=买入价，做空=卖出价）',
  `lots`       FLOAT       NOT NULL DEFAULT 1 COMMENT '手数',
  `multiplier` FLOAT       DEFAULT NULL COMMENT '每点价值（可选，不填则按品种自动查）',
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_positions_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='期货/股票持仓';

-- ---------------------------------------------------------------------
-- 4. watch_groups 看盘分组（按用户隔离）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `watch_groups`;
CREATE TABLE `watch_groups` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`    BIGINT      NOT NULL COMMENT '所属用户 id',
  `name`       VARCHAR(50) NOT NULL COMMENT '分组名',
  `codes`      JSON        NOT NULL COMMENT '品种代码数组（JSON）',
  `sort_order` INT         NOT NULL DEFAULT 0 COMMENT '排序序号（越小越靠前）',
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_watch_groups_user_name` (`user_id`, `name`),
  KEY `ix_watch_groups_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='看盘分组';

-- ---------------------------------------------------------------------
-- 5. settlements 结算记录（持仓平仓时的成交快照与盈亏）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `settlements`;
CREATE TABLE `settlements` (
  `id`           BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`      BIGINT      NOT NULL COMMENT '所属用户 id',
  `code`         VARCHAR(32) NOT NULL COMMENT '品种代码（如 nf_SA2701）',
  `direction`    VARCHAR(8)  NOT NULL DEFAULT 'long' COMMENT '开仓方向快照：long=做多 / short=做空',
  `name`         VARCHAR(64) DEFAULT NULL COMMENT '品种名称快照',
  `buy_price`    FLOAT       NOT NULL COMMENT '开仓价快照',
  `settle_price` FLOAT       NOT NULL COMMENT '结算价',
  `spread`       FLOAT       NOT NULL DEFAULT 0 COMMENT '按方向价差（多=结算价-开仓价，空=开仓价-结算价）',
  `lots`         FLOAT       NOT NULL DEFAULT 1 COMMENT '手数',
  `multiplier`   FLOAT       DEFAULT NULL COMMENT '每点价值快照',
  `pnl`          FLOAT       NOT NULL COMMENT '盈亏（已按方向计，再×手数×乘数）',
  `currency`     VARCHAR(8)  NOT NULL DEFAULT 'CNY' COMMENT '币种 CNY/USD/HKD',
  `settled_at`   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '结算时间',
  PRIMARY KEY (`id`),
  KEY `ix_settlements_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='结算记录';

-- ---------------------------------------------------------------------
-- 6. schema_migrations 手动迁移记录表（非 ORM 模型，历史 ALTER 留痕）
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `schema_migrations`;
CREATE TABLE `schema_migrations` (
  `version`    VARCHAR(64)  NOT NULL COMMENT '迁移版本号',
  `name`       VARCHAR(255) NOT NULL COMMENT '迁移文件名',
  `applied_at` DATETIME     NOT NULL COMMENT '应用时间',
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='手动迁移记录';
