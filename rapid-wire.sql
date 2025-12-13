SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";

--
-- Database: `rapid_wire`
--

-- --------------------------------------------------------

--
-- Table structure for table `api_key`
--

CREATE TABLE `api_key` (
  `user_id` bigint UNSIGNED NOT NULL,
  `api_key` char(24) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `balance`
--

CREATE TABLE `balance` (
  `user_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `claims`
--

CREATE TABLE `claims` (
  `claim_id` bigint UNSIGNED NOT NULL,
  `claimant_id` bigint UNSIGNED NOT NULL COMMENT '請求者',
  `payer_id` bigint UNSIGNED NOT NULL COMMENT '支払者',
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL,
  `status` enum('pending','paid','canceled') NOT NULL DEFAULT 'pending',
  `created_at` bigint UNSIGNED NOT NULL,
  `description` varchar(50) DEFAULT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `contract`
--

CREATE TABLE `contract` (
  `user_id` bigint UNSIGNED NOT NULL,
  `script` blob NOT NULL,
  `cost` int UNSIGNED NOT NULL,
  `max_cost` int UNSIGNED NOT NULL DEFAULT '0'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `currency`
--

CREATE TABLE `currency` (
  `currency_id` bigint UNSIGNED NOT NULL COMMENT 'Guild ID',
  `name` varchar(24) NOT NULL,
  `symbol` varchar(8) NOT NULL,
  `issuer` bigint UNSIGNED NOT NULL,
  `supply` decimal(24, 0) NOT NULL,
  `minting_renounced` tinyint(1) NOT NULL DEFAULT '0',
  `delete_requested_at` bigint UNSIGNED DEFAULT NULL,
  `daily_interest_rate` int UNSIGNED NOT NULL DEFAULT '0',
  `new_daily_interest_rate` int UNSIGNED DEFAULT NULL,
  `rate_change_requested_at` bigint UNSIGNED DEFAULT NULL,
  CHECK (`supply` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `staking`
--

CREATE TABLE `staking` (
  `user_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL,
  `last_updated_at` bigint UNSIGNED NOT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `liquidity_pool`
--

CREATE TABLE `liquidity_pool` (
  `pool_id` int UNSIGNED NOT NULL,
  `currency_a_id` bigint UNSIGNED NOT NULL,
  `currency_b_id` bigint UNSIGNED NOT NULL,
  `reserve_a` decimal(24, 0) NOT NULL,
  `reserve_b` decimal(24, 0) NOT NULL,
  `total_shares` decimal(24, 0) NOT NULL,
  CHECK (`reserve_a` >= 0),
  CHECK (`reserve_b` >= 0),
  CHECK (`total_shares` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `liquidity_provider`
--

CREATE TABLE `liquidity_provider` (
  `provider_id` int UNSIGNED NOT NULL,
  `pool_id` int UNSIGNED NOT NULL,
  `user_id` bigint UNSIGNED NOT NULL,
  `shares` decimal(24, 0) NOT NULL,
  CHECK (`shares` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `contract_int_variables`
--

CREATE TABLE `contract_int_variables` (
  `user_id` bigint UNSIGNED NOT NULL,
  `key` varchar(31) NOT NULL,
  `value` decimal(30, 0) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `contract_str_variables`
--

CREATE TABLE `contract_str_variables` (
  `user_id` bigint UNSIGNED NOT NULL,
  `key` varchar(31) NOT NULL,
  `value` varchar(127) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `notification_permissions`
--

CREATE TABLE `notification_permissions` (
  `user_id` bigint UNSIGNED NOT NULL,
  `allowed_user_id` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `discord_permissions`
--

CREATE TABLE `discord_permissions` (
  `guild_id` bigint UNSIGNED NOT NULL,
  `user_id` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
--
-- New Tables
--
-- --------------------------------------------------------

--
-- Table structure for table `execution`
--

CREATE TABLE `execution` (
  `execution_id` bigint UNSIGNED NOT NULL,
  `caller_id` bigint UNSIGNED NOT NULL COMMENT '操作を行ったユーザー (Sender)',
  `contract_owner_id` bigint UNSIGNED NOT NULL COMMENT '実行対象 (0=System, その他=User Contract)',
  `input_data` varchar(127) DEFAULT NULL COMMENT '入力データ または システムコマンド(例: update_contract)',
  `output_data` varchar(127) DEFAULT NULL COMMENT '返り値 または エラーメッセージ',
  `cost` int UNSIGNED NOT NULL DEFAULT '0',
  `status` enum('pending', 'success', 'failed', 'reverted') NOT NULL,
  `timestamp` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------

--
-- Table structure for table `transfer`
--

CREATE TABLE `transfer` (
  `transfer_id` bigint UNSIGNED NOT NULL,
  `execution_id` bigint UNSIGNED DEFAULT NULL COMMENT '親Execution ID (直接送金の場合はNULLも可)',
  `source_id` bigint UNSIGNED NOT NULL,
  `dest_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL,
  `timestamp` bigint UNSIGNED NOT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------

--
-- Table structure for table `contract_history`
--

CREATE TABLE `contract_history` (
  `history_id` bigint UNSIGNED NOT NULL,
  `execution_id` bigint UNSIGNED NOT NULL COMMENT '親Execution ID',
  `user_id` bigint UNSIGNED NOT NULL COMMENT 'コントラクトの所有者',
  `script_hash` binary(32) NOT NULL COMMENT 'SHA-256 Hash',
  `cost` int UNSIGNED NOT NULL COMMENT '計算されたコスト',
  `created_at` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------

--
-- Table structure for table `allowance`
--

CREATE TABLE `allowance` (
  `owner_id` bigint UNSIGNED NOT NULL,
  `spender_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL,
  `last_updated_at` bigint UNSIGNED NOT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------

--
-- Table structure for table `allowance_log`
--

CREATE TABLE `allowance_log` (
  `log_id` bigint UNSIGNED NOT NULL,
  `execution_id` bigint UNSIGNED DEFAULT NULL COMMENT '親Execution ID',
  `owner_id` bigint UNSIGNED NOT NULL,
  `spender_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` decimal(24, 0) NOT NULL COMMENT '設定された許可額',
  `timestamp` bigint UNSIGNED NOT NULL,
  CHECK (`amount` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

--
-- Indexes for dumped tables
--

--
-- Indexes for table `api_key`
--
ALTER TABLE `api_key`
  ADD PRIMARY KEY (`user_id`),
  ADD UNIQUE KEY `api_key` (`api_key`);

--
-- Indexes for table `balance`
--
ALTER TABLE `balance`
  ADD PRIMARY KEY (`user_id`,`currency_id`),
  ADD KEY `currency_id` (`currency_id`);

--
-- Indexes for table `claims`
--
ALTER TABLE `claims`
  ADD PRIMARY KEY (`claim_id`),
  ADD KEY `claimant_id` (`claimant_id`),
  ADD KEY `payer_id` (`payer_id`);

--
-- Indexes for table `contract`
--
ALTER TABLE `contract`
  ADD PRIMARY KEY (`user_id`);

--
-- Indexes for table `currency`
--
ALTER TABLE `currency`
  ADD PRIMARY KEY (`currency_id`),
  ADD UNIQUE KEY `symbol` (`symbol`);

--
-- Indexes for table `staking`
--
ALTER TABLE `staking`
  ADD PRIMARY KEY (`user_id`,`currency_id`),
  ADD KEY `currency_id` (`currency_id`);

--
-- Indexes for table `liquidity_pool`
--
ALTER TABLE `liquidity_pool`
  ADD PRIMARY KEY (`pool_id`),
  ADD UNIQUE KEY `currency_pair` (`currency_a_id`,`currency_b_id`);

--
-- Indexes for table `liquidity_provider`
--
ALTER TABLE `liquidity_provider`
  ADD PRIMARY KEY (`provider_id`),
  ADD UNIQUE KEY `pool_user` (`pool_id`,`user_id`);

--
-- Indexes for table `contract_int_variables`
--
ALTER TABLE `contract_int_variables`
  ADD PRIMARY KEY (`user_id`, `key`);

--
-- Indexes for table `contract_str_variables`
--
ALTER TABLE `contract_str_variables`
  ADD PRIMARY KEY (`user_id`, `key`);

--
-- Indexes for table `notification_permissions`
--
ALTER TABLE `notification_permissions`
  ADD PRIMARY KEY (`user_id`,`allowed_user_id`);

--
-- Indexes for table `discord_permissions`
--
ALTER TABLE `discord_permissions`
  ADD PRIMARY KEY (`guild_id`,`user_id`);

--
-- Indexes for new tables
--

--
-- Indexes for table `execution`
--
ALTER TABLE `execution`
  ADD PRIMARY KEY (`execution_id`),
  ADD KEY `caller_id` (`caller_id`),
  ADD KEY `contract_owner_id` (`contract_owner_id`),
  ADD KEY `timestamp` (`timestamp`);

--
-- Indexes for table `transfer`
--
ALTER TABLE `transfer`
  ADD PRIMARY KEY (`transfer_id`),
  ADD KEY `execution_id` (`execution_id`),
  ADD KEY `source_id` (`source_id`),
  ADD KEY `dest_id` (`dest_id`);

--
-- Indexes for table `contract_history`
--
ALTER TABLE `contract_history`
  ADD PRIMARY KEY (`history_id`),
  ADD KEY `execution_id` (`execution_id`),
  ADD KEY `user_id` (`user_id`);

--
-- Indexes for table `allowance`
--
ALTER TABLE `allowance`
  ADD PRIMARY KEY (`owner_id`, `spender_id`, `currency_id`);

--
-- Indexes for table `allowance_log`
--
ALTER TABLE `allowance_log`
  ADD PRIMARY KEY (`log_id`),
  ADD KEY `execution_id` (`execution_id`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `claims`
--
ALTER TABLE `claims`
  MODIFY `claim_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `liquidity_pool`
--
ALTER TABLE `liquidity_pool`
  MODIFY `pool_id` int UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `liquidity_provider`
--
ALTER TABLE `liquidity_provider`
  MODIFY `provider_id` int UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for new tables
--

--
-- AUTO_INCREMENT for table `execution`
--
ALTER TABLE `execution`
  MODIFY `execution_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `transfer`
--
ALTER TABLE `transfer`
  MODIFY `transfer_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `contract_history`
--
ALTER TABLE `contract_history`
  MODIFY `history_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `allowance_log`
--
ALTER TABLE `allowance_log`
  MODIFY `log_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

COMMIT;
