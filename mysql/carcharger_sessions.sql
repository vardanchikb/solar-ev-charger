CREATE DATABASE IF NOT EXISTS `carcharger`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `carcharger`;

CREATE TABLE IF NOT EXISTS `charging_sessions` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  started_at DATETIME NOT NULL,
  ended_at DATETIME NULL,
  timezone_name VARCHAR(64) NOT NULL,
  start_phase VARCHAR(96) NULL,
  end_phase VARCHAR(96) NULL,
  start_reason VARCHAR(255) NULL,
  end_reason VARCHAR(255) NULL,
  target_amps INT NULL,
  start_setpoint_amps INT NULL,
  end_setpoint_amps INT NULL,
  max_setpoint_amps INT NULL,
  max_actual_amps DECIMAL(8,2) NULL,
  max_power_w DECIMAL(12,2) NULL,
  end_power_w DECIMAL(12,2) NULL,
  energy_wh DECIMAL(14,3) NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_started_at (started_at),
  KEY idx_status (status)
);

CREATE TABLE IF NOT EXISTS `charger_telemetry_samples` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sampled_at DATETIME NOT NULL,
  timezone_name VARCHAR(64) NOT NULL,
  charger_state VARCHAR(64) NULL,
  pilot_state VARCHAR(64) NULL,
  enabled TINYINT(1) NULL,
  actively_charging TINYINT(1) NULL,
  status_quality VARCHAR(64) NULL,
  setpoint_amps INT NULL,
  dp9_power_w DECIMAL(12,2) NULL,
  dp6_raw VARCHAR(128) NULL,
  dp6_decoded_json LONGTEXT NULL,
  dp6_voltage_v DECIMAL(8,2) NULL,
  dp6_current_a DECIMAL(8,3) NULL,
  dp6_power_w DECIMAL(12,2) NULL,
  raw_dps_json LONGTEXT NULL,
  automation_phase VARCHAR(96) NULL,
  automation_reason VARCHAR(255) NULL,
  command_type VARCHAR(32) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_sampled_at (sampled_at),
  KEY idx_dp6_raw (dp6_raw),
  KEY idx_charger_state (charger_state)
);
