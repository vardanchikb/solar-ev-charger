CREATE DATABASE IF NOT EXISTS `carcharger`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `carcharger`;

CREATE TABLE IF NOT EXISTS `tesla_energy_live_samples` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sampled_at DATETIME NOT NULL,
  timezone_name VARCHAR(64) NOT NULL DEFAULT 'UTC',
  site_name VARCHAR(128) NULL,
  grid_status VARCHAR(32) NULL,
  storm_mode_active TINYINT(1) NOT NULL DEFAULT 0,
  solar_generation_w DECIMAL(12,2) NULL,
  home_consumption_w DECIMAL(12,2) NULL,
  powerwall_level_pct DECIMAL(6,2) NULL,
  grid_import_w DECIMAL(12,2) NULL,
  grid_export_w DECIMAL(12,2) NULL,
  raw_json LONGTEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sampled_at (sampled_at),
  KEY idx_sampled_at (sampled_at)
);

CREATE TABLE IF NOT EXISTS `tesla_energy_history` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  bucket_start DATETIME NOT NULL,
  bucket_end DATETIME NULL,
  period_name VARCHAR(16) NOT NULL,
  timezone_name VARCHAR(64) NOT NULL DEFAULT 'UTC',
  solar_generation_wh DECIMAL(14,3) NULL,
  home_consumption_wh DECIMAL(14,3) NULL,
  battery_charge_wh DECIMAL(14,3) NULL,
  battery_discharge_wh DECIMAL(14,3) NULL,
  grid_import_wh DECIMAL(14,3) NULL,
  grid_export_wh DECIMAL(14,3) NULL,
  raw_json LONGTEXT NULL,
  imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_bucket_period (bucket_start, period_name),
  KEY idx_bucket_start (bucket_start),
  KEY idx_period_name (period_name)
);
