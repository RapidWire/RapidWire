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
  `amount` bigint UNSIGNED NOT NULL
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
  `amount` bigint UNSIGNED NOT NULL,
  `status` enum('pending','paid','canceled') NOT NULL DEFAULT 'pending',
  `created_at` bigint UNSIGNED NOT NULL,
  `description` varchar(100) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `contract`
--

CREATE TABLE `contract` (
  `user_id` bigint UNSIGNED NOT NULL,
  `script` text NOT NULL,
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
  `supply` bigint UNSIGNED NOT NULL,
  `minting_renounced` tinyint(1) NOT NULL DEFAULT '0',
  `delete_requested_at` bigint UNSIGNED DEFAULT NULL,
  `daily_interest_rate` int UNSIGNED NOT NULL DEFAULT '0',
  `new_daily_interest_rate` int UNSIGNED DEFAULT NULL,
  `rate_change_requested_at` bigint UNSIGNED DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `staking`
--

CREATE TABLE `staking` (
  `user_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` bigint UNSIGNED NOT NULL,
  `last_updated_at` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `transaction`
--

CREATE TABLE `transaction` (
  `transaction_id` bigint UNSIGNED NOT NULL,
  `source` bigint UNSIGNED NOT NULL,
  `dest` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` bigint UNSIGNED NOT NULL,
  `inputData` varchar(16) DEFAULT NULL,
  `timestamp` bigint NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `liquidity_pool`
--

CREATE TABLE `liquidity_pool` (
  `pool_id` int UNSIGNED NOT NULL,
  `currency_a_id` bigint UNSIGNED NOT NULL,
  `currency_b_id` bigint UNSIGNED NOT NULL,
  `reserve_a` bigint UNSIGNED NOT NULL,
  `reserve_b` bigint UNSIGNED NOT NULL,
  `total_shares` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `liquidity_provider`
--

CREATE TABLE `liquidity_provider` (
  `provider_id` int UNSIGNED NOT NULL,
  `pool_id` int UNSIGNED NOT NULL,
  `user_id` bigint UNSIGNED NOT NULL,
  `shares` bigint UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


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
-- Indexes for table `transaction`
--
ALTER TABLE `transaction`
  ADD PRIMARY KEY (`transaction_id`),
  ADD KEY `source` (`source`),
  ADD KEY `dest` (`dest`),
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
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `claims`
--
ALTER TABLE `claims`
  MODIFY `claim_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for `transaction`
--
ALTER TABLE `transaction`
  MODIFY `transaction_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

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

COMMIT;