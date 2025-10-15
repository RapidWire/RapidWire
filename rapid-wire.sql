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
  `script` text NOT NULL
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
  `daily_interest_rate` decimal(10,9) NOT NULL DEFAULT '0.000000000'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `staking`
--

CREATE TABLE `staking` (
  `stake_id` bigint UNSIGNED NOT NULL,
  `user_id` bigint UNSIGNED NOT NULL,
  `currency_id` bigint UNSIGNED NOT NULL,
  `amount` bigint UNSIGNED NOT NULL,
  `staked_at` bigint UNSIGNED NOT NULL,
  `daily_interest_rate` decimal(10,9) NOT NULL
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
  ADD PRIMARY KEY (`stake_id`),
  ADD KEY `user_id` (`user_id`),
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
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `claims`
--
ALTER TABLE `claims`
  MODIFY `claim_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `staking`
--
ALTER TABLE `staking`
  MODIFY `stake_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `transaction`
--
ALTER TABLE `transaction`
  MODIFY `transaction_id` bigint UNSIGNED NOT NULL AUTO_INCREMENT;

-- --------------------------------------------------------

--
-- Stored Procedures
--

DELIMITER $$
CREATE DEFINER=`root`@`localhost` PROCEDURE `sp_update_interest_rate`(IN `p_currency_id` BIGINT UNSIGNED, IN `p_new_rate` DECIMAL(10,9))
BEGIN
    -- 変数の宣言
    DECLARE done INT DEFAULT FALSE;
    DECLARE v_stake_id BIGINT UNSIGNED;
    DECLARE v_user_id BIGINT UNSIGNED;
    DECLARE v_amount BIGINT UNSIGNED;
    DECLARE v_staked_at BIGINT UNSIGNED;
    DECLARE v_old_rate DECIMAL(10,9);
    DECLARE v_current_timestamp BIGINT UNSIGNED;
    DECLARE v_days_staked BIGINT UNSIGNED;
    DECLARE v_reward BIGINT UNSIGNED;
    DECLARE v_total_payout BIGINT UNSIGNED;
    DECLARE SYSTEM_USER_ID BIGINT UNSIGNED DEFAULT 0;
    DECLARE SECONDS_IN_A_DAY INT DEFAULT 86400;

    -- p_currency_id に関連する全てのステークを取得するカーソル
    DECLARE cur CURSOR FOR 
        SELECT stake_id, user_id, amount, staked_at, daily_interest_rate 
        FROM staking 
        WHERE currency_id = p_currency_id;

    -- カーソルが見つからなかった場合のためのハンドラ
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

    -- トランザクション開始
    START TRANSACTION;

    SET v_current_timestamp = UNIX_TIMESTAMP();

    -- カーソルを開く
    OPEN cur;

    read_loop: LOOP
        FETCH cur INTO v_stake_id, v_user_id, v_amount, v_staked_at, v_old_rate;
        IF done THEN
            LEAVE read_loop;
        END IF;

        -- 報酬の計算
        SET v_days_staked = FLOOR((v_current_timestamp - v_staked_at) / SECONDS_IN_A_DAY);
        IF v_days_staked > 0 THEN
            SET v_reward = FLOOR(v_amount * v_days_staked * v_old_rate);
        ELSE
            SET v_reward = 0;
        END IF;
        
        SET v_total_payout = v_amount + v_reward;

        -- ユーザーの残高に元本と報酬を戻す
        INSERT INTO balance (user_id, currency_id, amount)
        VALUES (v_user_id, p_currency_id, v_total_payout)
        ON DUPLICATE KEY UPDATE amount = amount + v_total_payout;

        -- システムアカウントの残高を減らす（報酬分）
        INSERT INTO balance (user_id, currency_id, amount)
        VALUES (SYSTEM_USER_ID, p_currency_id, v_reward)
        ON DUPLICATE KEY UPDATE amount = amount - v_reward;

        -- 取引履歴を作成
        INSERT INTO transaction (source, dest, currency_id, amount, inputData, timestamp)
        VALUES (SYSTEM_USER_ID, v_user_id, p_currency_id, v_total_payout, 'stake:re-stake', v_current_timestamp);

        -- 古いステークを削除
        DELETE FROM staking WHERE stake_id = v_stake_id;

        -- 新しい利率で再度ステーキング
        INSERT INTO staking (user_id, currency_id, amount, staked_at, daily_interest_rate)
        VALUES (v_user_id, p_currency_id, v_amount, v_current_timestamp, p_new_rate);
        
    END LOOP;

    -- カーソルを閉じる
    CLOSE cur;

    -- 通貨テーブルの利率を更新
    UPDATE currency SET daily_interest_rate = p_new_rate WHERE currency_id = p_currency_id;

    -- トランザクションをコミット
    COMMIT;
END$$
DELIMITER ;

COMMIT;
