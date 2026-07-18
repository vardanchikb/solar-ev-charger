# Carcharger Project Context

Use this file as the first-stop source of truth for future changes in this repo.

## Start Here

For future requests in this repo, start in this order:

1. Read this file first for the current app model and known behavior
2. Read `config.yaml` next if the request is about actual runtime behavior on this machine
3. Read `CONFIG_REFERENCE.md` if the request is about tuning or explaining automation config fields
4. If the issue is about what happened at a specific time, check `journalctl --user-unit carcharger-automation.service`
5. Only then open the directly affected source files

For charging-behavior issues specifically:

- Runtime config: `config.yaml`
- Operator-facing config explanation: `CONFIG_REFERENCE.md`
- Main logic: `solar_ev_controller.py`
- Service logs: `carcharger-automation.service`
- UI/API status surface: `web_config.py`
- Seasonal profile selection: `config.yaml` under `automation.active_profile`
- Seasonal automation configs: `config.yaml` under `automation.profiles.spring|summer|fall|winter`

For Tesla Fleet energy issues specifically:

- Runtime config: `config.yaml` under `tesla_energy`
- OAuth, token refresh, cache, and energy fetch logic: `tesla_energy.py`
- Localhost OAuth bootstrap helper: `tesla_local_auth.py`
- Background sync worker: `tesla_energy_sync.py`
- Settings/auth UI: `templates/system.html`
- Energy dashboard UI: `templates/dashboard_status.html`
- Energy controller and selected profile tuning config UI: `templates/charger_profiles.html`
- Cached Tesla site/history data: `tesla_energy_cache.json`
- Persistent Tesla history tables: `tesla_energy_live_samples` and `tesla_energy_history`

## Purpose

This project controls an Aimiler EV charger and exposes a local web UI for live status, emergency charging, charger configuration, and charging session history.

The app has two primary entrypoints:

- `solar_ev_controller.py`: charger integration, automation logic, emergency override, status reporting, and MySQL session logging
- `web_config.py`: Flask UI and JSON APIs for dashboard, charger settings, sessions, and emergency actions

## Main Runtime Pieces

### 1. Charger control

`AimilerCharger` in `solar_ev_controller.py` talks to the charger over Tuya via `tinytuya`.

Known current defaults from this repo:

- Tuya version: `3.4`
- Switch DP: `18`
- Current DP: `4`
- Mode DP: `14`
- Min amps: `8`
- Max amps: `32`
- Tuya socket persistence is optional. The live config intentionally uses
  `charger.socket_persistent: true` with
  `socket_persistent_recycle_seconds: 900` (re-enabled after the June 1, 2026
  revert; the operator confirmed keeping persistent sockets on June 10, 2026).
  Persistent sockets have a known stale-status blind spot: full `status()`
  reads can return frozen DP snapshots for minutes after a command (confirmed
  June 1, 2026 from `09:49` to `10:03`, and again June 10, 2026 when full reads
  reported `charger_charging`/`1887W` for ~7 minutes after a STOP while async
  partial packets showed the true stopped state). To compensate, all
  verification reads before repeat/duplicate START/STOP commands use
  `AimilerCharger.fresh_status()`, which closes the persistent socket first and
  reads on a brand-new socket with no cached-status merging.

#### Aimiler UA_7KW Charger Reference

Use this block as the single place for observed Aimiler/Tuya charger details.

Hardware/protocol observed on this machine:

- Model/name: `Aimiler EV Charger(UA_7KW)`
- Firmware string observed in DP `23`: `B5_V1.0.0`
- Tuya protocol version: `3.4`
- Local IP is environment-specific and lives in `config.yaml` under `charger.ip`
- `tinytuya.Device.status()` is the normal status read. `updatedps()` and
  `detect_available_dps()` were tested during the May 18, 2026 DP18 issue and
  did not recover the missing switch DP.
- TCP socket handling is configurable. When `charger.socket_persistent: false`,
  every charger status read and every write command closes the local Tuya socket
  in a `finally` block. When `true`, the controller keeps the Tinytuya socket
  open and logs socket diagnostics in `tuya_raw_debug.jsonl`; age-based recycling
  is controlled by `charger.socket_persistent_recycle_seconds`.
- Raw Tuya response collection is controlled by `charger.debug_tuya_responses`
  and exposed in `/system` as **Debug Tuya Responses**. Saving the System page
  writes the flag to `config.yaml`; the automation service picks it up on the
  next config reload/control loop. On June 1, 2026 at about `12:47` local time,
  raw recording was temporarily re-enabled for a multi-day diagnostic capture
  while short-lived sockets remain active.
- With persistent sockets, Tuya can send single-DP async update packets on the
  same channel. Raw log entries with `operation: status_raw_partial` are these
  delta packets, not full charger snapshots. The controller logs the merged view
  it actually used as `operation: status_effective`. Charger write commands
  intentionally close the persistent read socket first and use a fresh socket so
  stale async packets are not mistaken for write acknowledgements.
- Persistent sockets must be watched carefully. Keeping a local Tuya TCP session
  open across polls previously appeared correlated with more `914` ("Check
  device key or version") errors, and the June 1, 2026 incident showed a
  stale-status blind spot during the persistent-socket test. The live config now
  uses short-lived sockets despite the repeated Tuya 3.4 session-key handshakes.


Configured control DPs:

- DP `18`: output switch/enable. Used for START/STOP commands. Treat as
  important but not sufficient by itself, because it can be missing after a
  charger restart and can briefly disagree with active charging reports.
- DP `4`: charger current setpoint in amps. Runtime min/max are `8A`/`32A`.
- DP `14`: charging mode. Normal automation mode is `charge_now`.

Observed status DPs and meanings:

- DP `1`: `forward_energy_total`, total forward active energy. Tuya Cloud reports
  unit `kW·h` with scale `2`.
- DP `3`: charger state string, such as `charger_free`, `charger_insert`, or
  `charger_charging`.
- DP `9`: reported charger power in watts. Values above about `100W` are treated
  as active charging. Tuya Cloud names this `power_total` with unit `kW` and
  scale `3`, so raw local value `2148` means about `2.148kW`.
- DP `10`: `fault` bitmap. Non-zero values are fault/interlock signals.
- DP `13`: pilot/control state. Common values include `controlpi_12v`,
  `controlpi_9v`, `controlpi_9v_pwm`, `controlpi_6v`, and
  `controlpi_6v_pwm`. `controlpi_6v_pwm` is an active charging signal.
- DP `19`: `local_timer`, raw timer payload.
- DP `23`: firmware string.
- DP `24`: `temp_current`, current temperature in Celsius.
- DP `25`: `charge_energy_once`, single-session energy. Tuya Cloud reports unit
  `kW·h` with scale `2`.
- DP `27`: `online_state`, enum reported by Tuya Cloud.
- DP `28`: `timer_on`, timer enable/value.
- DP `33`: `mode_set`, raw mode/config payload. Example observed value:
  `AQAAAQEAAAA=`. Do not treat this as a fault trigger by itself.
- DP `101`: `F_temp`, Fahrenheit temperature.
- DP `103`: `Residual_delaytime`, residual delay time.
- DP `104`: `charger_time`, charger time/duration metric.
- DP `105`: `quick_start`, boolean flag, currently not used for control decisions.
- DP `106`: `charge_capacity`, charge-capacity metric.
- DP `107`: `charge_chart`, chart/status metric. Historical values have varied
  during charging, so do not key active charging on DP `107` alone.
- DP `108`: `charge_record`, raw rolling charge-record payload. Observed layout:
  two encoded timestamps followed by two 16-bit values. Example:
  `GgUYCSIaBRgJJwAEAAs=` -> `2026-05-24 09:34` to `2026-05-24 09:39`,
  tail values `4` and `11`, matching the later `charger_time`/chart-capacity
  metrics. Record it, but do not treat it as a reset trigger by itself.
- DP `109`: temperature unit string, usually `temF`.
- DP `120`: `error_data`, diagnostic string such as
  `V1:0_0V, A1:0_0.0A, T1:0_45.0, G1:0_499, CP1:1_0.4V`.
- DP `121`: `cp_data`, CP metric reported by Tuya Cloud.
- DP `6`: observed packed electrical telemetry. The base64 payload decodes as
  8 bytes: bytes `0-1` are voltage in tenths of a volt, bytes `2-4` are current
  in milliamps, and bytes `5-7` are power in watts. Example:
  `CVoAAAAAAAA=` -> `09 5a 00 00 00 00 00 00` -> about `239.4V`, `0.000A`,
  `0W`. DP `6` appears during normal idle and charging states and must not be
  treated as a fault trigger by itself.

Observed state signatures:

- Active charging: usually DP `3=charger_charging`, DP `13=controlpi_6v_pwm`,
  DP `9 > 100W`, and DP `18=true` when the switch DP is healthy. Historical
  DP `107` values have varied during charging, so do not key active charging
  on DP `107` alone.
- Connected or inserted but not charging: often DP `3=charger_insert`,
  DP `13=controlpi_9v` or `controlpi_9v_pwm`, and DP `9=0`.
- Free/disconnected idle: often DP `3=charger_free`, DP `13=controlpi_12v`,
  and DP `9=0`.
- Output enabled but no vehicle request: can look like `charger_free` plus
  `controlpi_12v` with DP `18=true`; automation should wait for vehicle
  connection instead of treating this as a charger fault.

Known Aimiler/Tuya reporting quirks:

- After a charger restart, local Tuya can temporarily return error `914`
  (`Check device key or version`) before status reads recover. The controller
  recreates the Tinytuya object and retries once.
- On May 18, 2026, the charger returned payloads with core observation DPs
  (`3`, `4`, `9`, `13`, `14`) but omitted DP `18`. Automation now accepts this
  as live degraded status instead of full `CONTROL_UNAVAILABLE`; dashboard
  fields `switch_state_known` and `status_quality` show the condition.
- DP `18` can also briefly report `false` while DP `3`, DP `9`, and DP `13`
  still show charging. Treat that as contradictory status and combine it with
  Tesla home load plus command history before deciding whether the EV is really
  drawing power.
- After STOP, the charger may physically stop quickly but continue reporting
  `charger_charging`, `controlpi_6v_pwm`, or non-zero DP `9` for one or more
  polls. If Tesla home load has dropped below an EV-sized load after the STOP,
  automation suppresses those stale active/power DPs as post-stop settling.
- After START, the car/charger can be slow to wake or report. A recent START
  plus an EV-sized Tesla home-load increase can infer active charging when the
  charger DPs are degraded or contradictory.
- Cache merging is intentionally conservative: if a fresh partial payload says
  DP `18=false`, cached charging DPs must not be inherited as if charging is
  still live.


### 2. Automation

`AutoScheduleController` in `solar_ev_controller.py` owns automatic charging decisions.

Current behavior:

- Charging automation now supports seasonal profiles named `spring`, `summer`, `fall`, and `winter`
- The active seasonal profile is selected by `automation.active_profile`
- Each seasonal profile stores its own automation settings under `automation.profiles`
- Each seasonal profile can also store a live-reloadable `energy_controller` block under `automation.profiles.<profile>.energy_controller`
- The automation service reloads `config.yaml` when it changes, so a profile switch from the web UI applies on the next poll
- Daytime solar charging is now driven by a dedicated energy decision engine that uses cached Tesla telemetry plus predictive HVAC load adjustments
- `16:00-21:00` is a no-charge window unless emergency override is active
- During the `16:00-21:00` window, the energy controller can continue solar-follow charging if Tesla reports the Powerwall at full SOC. This is controlled by the active profile energy settings `enable_after_4pm_full_powerwall_solar_charging` and `after_4pm_full_powerwall_export_buffer_watts`; the `/charger` profile UI exposes this as fixed export-buffer ranges such as `200-300 W`.
- From `21:00` until `03:00`, the controller targets `32A` when the vehicle is connected
- If the vehicle is connected any time between `21:00` and `03:00`, the controller may start charging then
- If the vehicle is newly connected after `03:00`, it waits for daytime solar/cloudy-fallback logic instead of starting night charging
- If night charging was already active before `03:00` and the car is still actively charging, it can continue at the night target after `03:00`
- Emergency override charges at configured max amps
- The dashboard has a separate **Stop Charging To Unplug** action for ordinary
  charging. It stops output immediately and holds automation off until the
  vehicle disconnects, with a one-hour expiry so a missed disconnect poll cannot
  suppress charging indefinitely. Ending emergency charging uses the same
  one-hour safety expiry.
- Non-emergency automation uses an `automation_min_amps` floor. The active
  summer profile currently uses `8A` for solar/cloudy daytime charging; other
  profiles generally use `10A`.
- The controller now uses one shared `charger_command_min_interval_seconds`
  cooldown across all charger commands, including amp changes and start/stop actions
- The decision engine is pure and separate from the charger command executor
- If the controller requests charging but charging does not actually start within
  `startup_grace_seconds`, it enters a `startup_failure_cooldown_seconds` retry
  cooldown instead of re-enabling the charger every poll while solar targets move.
  The active summer profile currently uses a longer `120` second startup grace
  and `180` second retry cooldown so a slow car wakeup or reconnect does not
  create a 15-minute daytime block.
- If the cable is inserted but the car has not requested current yet,
  automation keeps offering charger output and reports
  `waiting_for_vehicle_request` instead of turning output back off at startup
  grace expiry.
- If the charger output is already enabled but Aimiler reports
  `charger_free/controlpi_12v`, automation keeps output enabled and reports
  `waiting_for_vehicle_connection` instead of entering startup retry cooldown.
- Suspected charger fault/interlock events are logged to
  `charger_event_log.jsonl`, and the controller performs one soft reset attempt
  per `fault_reset_cooldown_seconds` only when charger state text reports a
  fault/alarm or Tuya Cloud DP `10` reports a non-zero fault bitmap. DP `6` is
  decoded voltage/current/power telemetry, DP `33` is `mode_set`, and DP `108`
  is `charge_record`; none of those raw payloads triggers a reset by itself. A
  normal connected-but-disabled charger is not reset automatically.
- When charger status polling fails, the service now saves a stale status report
  with a dashboard-visible error banner. Wi-Fi/LAN outages are labeled as a
  charger connection error with restart-device guidance, while Tuya `914`
  key/version failures are labeled separately as a local auth/version mismatch.
- On a charger status/control failure, the automation loop records consecutive
  fetch error details in `automation_state.yaml`, recreates the Tinytuya charger
  object, and retries the cycle once even when rediscovery finds the same IP.
  If retry still fails, `/dashboard` marks the controller as
  `CONTROL_UNAVAILABLE`, shows a prominent charger-control error banner, and
  treats displayed charger values as stale rather than a live control decision.
- If Aimiler omits only the switch DP (`18`) but still returns the core
  observation DPs (`3`, `4`, `9`, `13`, `14`), automation treats the payload as
  live degraded status instead of a full control failure. The dashboard keeps
  `switch_state_known` and `status_quality` visible.
- During degraded or contradictory charger status, automation cross-checks Tesla
  home load, the most recent charger command, and the current setpoint. A recent
  START plus EV-sized Tesla load can mark charging as inferred active; a recent
  STOP plus Tesla load dropping below EV-sized load suppresses stale
  `charger_charging`/power DPs as post-stop settling.
- "Recent START" for Tesla home-load charging inference is bounded by
  `START_INFERENCE_WINDOW_SECONDS` (600s). Before June 10, 2026 the recency gate
  was unbounded, so a START from the previous night kept the inference armed
  forever and every morning appliance spike (>~1kW vs the previous loop's
  non-EV baseline) hallucinated active charging and produced phantom
  `external_start_*` STOP commands against a charger that Tuya cleanly reported
  as free (confirmed daily June 4–10, 2026).
- A vehicle connection is treated as "new" only on the edge: the controller
  stores `control.vehicle_present` in `automation_state.yaml` and flags
  `new_vehicle_connection` only on the absent→present transition without an
  active session. A car sitting plugged in without a session no longer bypasses
  the low-solar grace counter on every loop.
- A low-solar session is classified as externally started (which bypasses the
  `energy_down_sustain_seconds` down-hold) only when there was no controller
  START within `START_INFERENCE_WINDOW_SECONDS`. On June 10, 2026 at `09:30` the
  controller stopped its own 4-minute-old session on a single one-sample home
  spike because the session was misclassified as external; this rule prevents
  that.
- Before re-sending a retry START/STOP (`retry_after_stale_status`) or a
  duplicate STOP, the executor confirms the charger state with
  `fresh_status()` (new socket, no cached merge) and skips the command when the
  fresh read shows it is no longer needed.
- The per-loop decision log line is printed only when the
  phase/action/reason/command signature changes, when a command is actually
  sent, or every 300 seconds as a heartbeat, instead of every loop.
- `energy_decision_trace.jsonl` is pruned to a 3-day retention
  (`ENERGY_TRACE_RETENTION_SECONDS`) at most every 6 hours during trace
  appends.

Recent confirmed production issue and fix:

- On March 21, 2026 around `06:20` local time, the car started charging because night-charge force-enable was still effectively armed after the old cutoff
- The current night window now runs `21:00-03:00`; after `03:00`, night charging does not start and the controller returns to daytime logic

Shared-load rules:

- Outside Tesla solar control, if Nest cooling is active, non-emergency
  EV charging is reduced to the automation safety minimum
- The energy controller uses the latest MySQL Tesla live sample in `tesla_energy_live_samples`; it does not call Tesla live from the control loop
- Tesla `home_consumption_w` already includes HVAC and EV load, so the controller subtracts only current EV load before computing a new charger target
- Current EV load for the non-EV calculation is estimated using a **setpoint history lookup**, not the raw Tuya `power_w`, unless charger actual power is safely settled. When a new Tesla snapshot arrives, the system looks up which setpoint was active at the snapshot timestamp from a rolling `setpoint_history` list (last 5 commands). This prevents the temporal mismatch bug where `power_w` (current) minus `house_consumption` (9-min-old snapshot) produces phantom non-EV load. After a `SET_AMPS` or `START` command has been stable for `charger_command_min_interval_seconds` and the Tesla sample is not older than that command, lower actual charger watts can be used to detect vehicle-side caps such as a PHEV accepting less than the offered current. If a STOP/START sequence happens while a Tesla sample is stale, `ev_active_since` is advanced to the latest START so pre-START Tesla samples are not treated as containing EV load. The full details and fix history are in `ALGORITHM.md`.
- Predictive HVAC transition timestamps are only updated on real state changes. Repeated `on` polls are continuation, not a new appliance-start event.
- Predictive HVAC transition timestamps are cleared once a newer Tesla sample arrives that post-dates the transition (with a 30-second tolerance window to catch near-simultaneous events), so Tesla returns to being the sole source of truth after telemetry catches up while the current on/off state is preserved
- Predictive HVAC load uses `3600W` for cooling and `500W` for heating when that HVAC transition happened after the latest Tesla sample
- The older `/charger` solar-policy shortcut form has been replaced by the profile-only energy-controller editor. System settings moved to `/system`.
- The `/charger` profile editor now also exposes selected profile-level tuning
  fields such as `energy_down_sustain_seconds`, startup grace/cooldown,
  and automation poll/min-amp settings. The API keeps these at the profile level
  instead of saving them into the nested `energy_controller` block.
- Before `16:00`, daytime solar-follow uses `solar_buffer_min_watts` and
  `solar_buffer_max_watts` as a direct deadband over
  `solar_generation_w - home_consumption_w`: below min lowers by about half of
  the excess amps, rounded up, with at least a 1A decrease and stop-at-minimum
  behavior; between min/max holds; above max raises by about half of the
  remaining available amp headroom, rounded up, with at least a 1A increase.
  Powerwall SOC does not alter this pre-4PM math. The config/UI require at
  least a `300W` gap between min and max to avoid chatter.
- If Aimiler first reports active EV charging after the latest Tesla sample,
  that sample is treated as not containing EV load. The controller uses the
  same start-margin test it would use for a stopped charger, and an externally
  started low-solar session bypasses the down-hold so the charger can be stopped
  immediately.
- If the last charger command was `STOP` and Aimiler reports output disabled,
  lingering `active`/`power` readings are treated as post-command settling, not
  EV load available for solar math. This prevents a false solar-follow START
  while the charger or car is slow to report stopped.
- Energy-controller downward amp changes and low-solar stops are held for
  `energy_down_sustain_seconds` before being applied. The default is 180 seconds
  so short home-load spikes such as a coffee maker do not immediately pull EV
  charging down. Confirmed safety stops such as no-charge windows, emergency
  low-SOC stops, confirmed missing integrations, known predictive loads, or
  externally started low-solar sessions are not held by this setting.
- Legacy `solar_gap_boost_watts` and `solar_gap_trim_watts` fields are retained
  for compatibility, but pre-4PM solar-follow is governed by the min/max solar
  margin cap fields above. Outside the pre-4PM min/max deadband path, low-gap
  step-downs now move at least halfway toward the calculated target, bounded by
  the configured ramp-down limit, instead of trimming only 1A. The `/charger` UI
  hides those legacy gap fields from the normal profile editor so the active
  daytime knobs are unambiguous.
- A configurable after-4PM full-Powerwall rule can allow EV charging while leaving a fixed export buffer even during the normal `16:00-21:00` no-charge window.
- Cloudy fallback is explicitly blocked during the `16:00-21:00`
  no-charge window. The only non-emergency exception in that window is the
  full-Powerwall solar-follow rule, and it requires positive solar generation.
- Daytime solar/cloudy charging waits for a vehicle connection before issuing
  charger start commands. If the charger reports `charger_free/controlpi_12v`,
  automation reports `WAITING_FOR_VEHICLE_CONNECTION` instead of retrying
  `START` every command cooldown.
- When enabling a disabled charger and the desired target amps differ from the
  current setpoint, the command executor sets the target amps first and then
  enables output in the same command cycle. This prevents night charging from
  repeatedly sending `START` while the charger is still at the daytime `8A`
  setpoint.
- If a STOP was already sent and Aimiler reports the output switch disabled but
  still reports active charging/power, automation waits through the shared
  `charger_command_min_interval_seconds` settling window instead of re-sending
  STOP or re-starting solar-follow from the stale active reading.
- Low-solar grace counters are preserved through the `energy_down_sustain_seconds`
  hold, so the 180-second down-hold expires once and releases a STOP instead of
  restarting grace every loop.
- Stop commands are retried even during the normal command cooldown when the
  desired state is off but the charger still reports active charging/power.
- Before sending a repeat stop within 180 seconds of the previous STOP, the
  controller performs a fresh Tuya status read without cached-status merging. If
  that fresh read shows charging has already stopped, it suppresses the duplicate
  STOP.

### 3. Web app

`web_config.py` serves Flask pages and APIs on port `8788`.

All routes are protected with HTTP Basic Auth when `.secrets/dashboard_password`
exists (any username; password read from that file, which is the case on this
deployment). For local curl testing read the password first, e.g.
`curl -u "x:$(cat .secrets/dashboard_password)" http://127.0.0.1:8788/api/status`.
NEVER send state-changing POSTs to the live dashboard while testing.

Main routes:

- `/dashboard`: live status, emergency controls (including "charge N kWh"
  energy-target requests via `POST /api/energy/emergency/start` with
  `{"target_kwh": N}`; on completion `night.start_blocked_until_disconnect`
  suppresses night auto-charging until the vehicle is unplugged, while solar
  charging stays available), Tesla energy chart, and a temporary controller-trace debug chart
- `/charger`: summer/winter charger profile logic settings, with inline explanations for energy-controller and selected profile-level tuning fields
- `/system`: Aimiler/Tuya hardware, local integration endpoints, database, Tesla, and other system settings
- `/sessions`: recent charging history
- `/api/status`: live status report, with cached fallback
- `/api/energy/config`: current effective energy-controller config plus defaults
- `/api/energy/config/reset`: reset the active profile energy-controller config to safe defaults
- `/api/energy/status`: live energy-controller status, predictive-load details, cooldown, and last decision
- `/api/energy/debug-trace`: controller decision samples and actual charger
  command events for dashboard debugging. It accepts bounded `start` and `end`
  timestamps so the dashboard can align the trace with the Tesla chart. Long
  windows are sampled across the full interval while preserving command events.
- `/api/energy/decision/preview`: preview a hypothetical decision without touching the charger
- `/api/energy/emergency/start`: enable emergency charging mode in persistent config/state
- `/api/energy/emergency/stop`: disable emergency charging mode in persistent config/state
- `/api/energy/stop-for-unplug`: stop current charging until vehicle disconnect, with a one-hour safety expiry
- `/api/tesla-energy/status`: Tesla Fleet energy status and cached history
- `/api/tesla-energy/history`: Tesla chart data by preset or custom interval
- `/api/tesla-energy/import-history`: import Tesla historical energy data into MySQL
- `/api/sessions`: DB-backed session history and summary
- `/api/emergency/start`: turn on emergency charging
- `/api/emergency/stop`: stop emergency charging
- `/tesla/callback`: OAuth callback for Tesla code exchange when redirect URI points to the dashboard host

### 4. Database logging

`MySQLSessionStore` in `solar_ev_controller.py` records observed charging sessions.

Configuration lives under the `database:` block in `config.yaml`.

Important detail:

- DB password is stored in a local secret file, not directly in YAML
- Default password file comes from `DEFAULT_DB_PASSWORD_FILE`
- Schema bootstrap SQL lives in `mysql/carcharger_sessions.sql`
- Session `start_reason` and `end_reason` columns are widened to `VARCHAR(255)`
  because energy-controller reason strings can exceed the original 64-character
  limit.
- When database logging is enabled, charger status samples are also written to
  `charger_telemetry_samples` by default. Each sample stores raw DPS JSON plus
  DP `6` raw base64 and decoded voltage/current/power fields for troubleshooting
  and future logic refinement.

### 5. Tesla Fleet energy monitoring

`tesla_energy.py` handles Tesla Fleet OAuth, token refresh, energy site discovery, live site reads, and local time-series caching for the dashboard chart.

Current Tesla UI behavior:

- Tesla settings live on `/system`
- Energy-controller profile config lives on `/charger`
- Tesla partner-domain registration can be triggered from `/system` or `/api/tesla-energy/register-partner`
- The dashboard shows solar generation, Powerwall level, home consumption, grid export, and grid import
- The chart uses one combined view with different colors, with Powerwall percent on its own right-side scale
- The dashboard reads Tesla data from DB/cache only; it does not call Tesla live on page load
- The dashboard status API uses the automation status cache when it is fresh, but refreshes charger/status data when the cache is older than 90 seconds. The web does not make independent charger queries during normal operation — it trusts the automation loop's 60-second write cycle to keep the cache fresh. Direct charger queries from the web only happen if the automation loop has been stalled for >90 seconds.
- The dashboard defaults to a `6h` chart range and supports `2h`, `4h`, `6h`,
  `12h`, `24h`, `2d`, and custom date-time ranges
- The Tesla energy chart and controller decision chart share the same range
  controls and explicit timeline bounds, so timestamps line up horizontally
  across both dashboard graphs.
- Tesla OAuth bootstrap is designed for `http://localhost:5000/callback` via `tesla_local_auth.py`
- Tesla chart views now show all stored points for the selected range instead of re-bucketing them again in the dashboard layer
- Tesla live samples are stored in MySQL in `tesla_energy_live_samples`
- Imported Tesla history is stored in MySQL in `tesla_energy_history`
- Dashboard chart ranges support `2h`, `4h`, `6h`, `12h`, and custom start/end date-times
- Imported historical ranges use Tesla energy buckets; Powerwall percentage is only available for live-sampled ranges
- Tesla sync polls 24/7. The solar-hours window restriction
  (`sync_active_start_hour`/`sync_active_end_hour`) was removed on June 10,
  2026 because Tesla request volume is no longer treated as a constraint. Live
  polling runs continuously at `temporary_live_poll_interval_seconds: 30` with
  `temporary_allow_request_budget_overrun: true`; these are now the standing
  production settings, not a temporary test.
- The monthly request budget fields (`monthly_request_budget`,
  `monthly_request_reserve`) still exist and requests are still counted, but
  with the overrun flag enabled they no longer throttle or stop live polling.
- The automatic daily Tesla history import was removed from the sync loop on
  June 10, 2026. Live samples land in MySQL continuously; history can still be
  imported manually via `/api/tesla-energy/import-history`.
- The sync worker now logs every failed pull to journald
  (`tesla_sync_error consecutive=N error=...`, capped after 10 in a row) and a
  recovery line, retries failed pulls after 10 seconds (first 6 failures) then
  every 30 seconds, and records `sync_failing_since` /
  `consecutive_sync_failures` in `tesla_energy_sync_state.json`. The dashboard
  shows a warning banner when 3 or more consecutive Tesla pulls have failed.
  Context: on June 10, 2026 Tesla's `live_status` endpoint failed from `09:09`
  to `10:59` PDT (`powergate ... operation_timedout`), producing an 85-minute
  sample gap and a `tesla_telemetry_critical_stale_stop`; before this change
  the failures were completely invisible in service logs.
- Temporary Tesla query-limit testing is controlled by
  `tesla_energy.temporary_live_poll_interval_seconds` and
  `tesla_energy.temporary_allow_request_budget_overrun`. When enabled, live
  Powerwall reads use the fixed interval and can continue after the local budget
  is exhausted. Leave both disabled for normal budget enforcement. The May 30,
  2026 production test is intentionally using `30` seconds and `true` until the
  operator asks to stop.
- A spike confirmation mechanism allows the automation controller to request an early Tesla refresh: when an unexplained non-EV load increases by >500W in a new telemetry snapshot and HVAC is not reporting active, the controller writes `tesla_refresh_request.json`. The sync service wakes for the due request, pulling fresh data about 2 minutes after the spike. This prevents step-downs from brief appliance spikes that clear before the next normal pull.
- Tesla request accounting records successful requests and billable client-side API errors so the local budget does not undercount usage
- When a new Tesla sample shows an unexplained non-EV load spike above 500W
  and HVAC is not reporting active, the controller writes
  `tesla_refresh_request.json` for a confirmation pull about 2 minutes later.
  The Tesla sync worker wakes for due confirmation requests instead of sleeping
  through the normal budget-spaced interval, while Fleet request accounting still
  enforces the monthly usable budget/reserve.
- A user-space public gateway can expose Tesla paths without root Apache changes:
  - local HTTP listener on `8078`
  - local HTTPS listener on `9443`
  - intended public NAT mapping is `80 -> 8078` and `443 -> 9443`
  - HTTP redirects include the configured non-default HTTPS port, so local `443` stays available for other apps

Current known Tesla production state on this machine:

- OAuth is completed and the refresh token is stored locally
- The local partner public key file exists at `/var/www/tesla/.well-known/appspecific/com.tesla.3p.public-key.pem`
- `partner_domain` is configured as `your-domain.example`
- Tesla Fleet reads are still blocked until the Tesla developer app `allowed_origins` includes the matching HTTPS root domain and Tesla can reach the public key URL over the public internet
- A Let’s Encrypt webroot test on March 22, 2026 failed with `Timeout during connect` for `http://your-domain.example/.well-known/acme-challenge/...`, so public inbound port `80` is currently not reachable from the internet
- On March 22, 2026 Tesla partner registration was tested with `your-domain.example:7443` and Tesla rejected it as an invalid domain, so custom public ports are not supported for the `domain` registration field
- On March 22, 2026 `carcharger-tesla-public-gateway.service` was added and confirmed to serve the Tesla public key locally on `http://127.0.0.1:8078/.well-known/appspecific/com.tesla.3p.public-key.pem`
- On April 25, 2026 the live Tesla public gateway HTTPS port was moved from local `443` to local `9443` to leave local `443` and `7443` available for other apps
- On April 25, 2026 Tesla history import was updated for the current Fleet API behavior: history requests use RFC3339 timestamps and request `period=month`, which returns 30-minute energy buckets for this site
- On April 25, 2026 a 90-day Tesla history import succeeded with `1192` buckets inserted into `tesla_energy_history`

## Config And State Files

Primary files used at runtime:

- `config.yaml`: main charger, automation, and DB configuration
- `config.yaml`: seasonal automation profiles now live under `automation.profiles`
- `config.yaml`: daytime/night/emergency controller settings can now also live under `automation.profiles.<profile>.energy_controller`
- `automation_state.yaml`: emergency, startup, night, and active-session state
- `charger_status_cache.json`: cached low-level charger status
- `status_report_cache.json`: cached dashboard/status API report
- `tesla_energy_cache.json`: cached Tesla metrics and chart history
- `tesla_energy_sync_state.json`: Tesla request-budget and sync state
- `.secrets/mysql_password`: default local DB password file
- `.secrets/tesla_client_secret`: Tesla OAuth client secret
- `.secrets/tesla_partner_private_key.pem`: Tesla partner EC private key for the hosted public key
- `.secrets/tesla_tokens.json`: Tesla refresh/access token store
- `mysql/tesla_energy_history.sql`: Tesla live/history MySQL schema

Note:

- `config.example.yaml` is the sample config tracked in git
- `config.yaml` may exist locally and should be treated as environment-specific

## External Dependencies

The app depends on these local or LAN integrations when configured:

- Aimiler/Tuya charger on local network
- Nest HVAC status endpoint, default `http://127.0.0.1:8789/api/hvac-status`
- Local MySQL via CLI and socket, default `/run/mysqld/mysqld.sock`
- Tesla Fleet API for Powerwall and site energy data

## Removed / Dead Config Fields

These fields are silently dropped by `validate_energy_config` if found in old saved configs:

- `pre_4pm_consumption_cap_pct` — removed. Replaced by watt-based pre-4PM
  solar margin caps.
- `after_4pm_full_powerwall_generation_pct` — removed. Was never wired into any decision logic.

Before 4PM, `solar_buffer_min_watts` and `solar_buffer_max_watts` are direct
solar margin caps, not SOC-interpolated values. `solar_buffer_watts` remains as
an after-4PM/fallback buffer.

The `tesla_solar_*` profile fields were removed from the live `config.yaml`
summer profile on June 10, 2026. The `_apply_summer_tesla_solar_policy` /
`_apply_solar_hysteresis` code paths that read them are unreachable (they
require `base_phase == "day_solar"`, which is never set), so the fields only
created tuning confusion. The loader still accepts them with defaults if an old
config carries them.

`tesla_energy.sync_active_start_hour` / `sync_active_end_hour` were removed
from the live config on June 10, 2026 — the sync loop no longer has a
night-time window restriction.

## Energy Decision Algorithm

The decision engine is documented in `ALGORITHM.md`. Read that file before making any changes to `energy_controller.py` or the charger setpoint / non-EV estimation logic.

## Files That Matter Most For Changes

- `solar_ev_controller.py`: all charger behavior and automation rules
- `web_config.py`: Flask routes, settings persistence, emergency endpoints
- `tesla_energy.py`: Tesla OAuth flow, token refresh, site reads, and cached chart history
- `tesla_local_auth.py`: one-time localhost OAuth callback helper
- `tesla_energy_sync.py`: background Tesla sync worker with monthly request budgeting
- `tesla_public_gateway.py`: user-space HTTP/HTTPS gateway for Tesla `.well-known` hosting and HTTPS proxying
- `scripts/setup_tesla_partner_assets.sh`: creates the Tesla EC keypair and writes the hosted public key file
- `scripts/issue_tesla_gateway_cert.sh`: requests and installs the Let's Encrypt cert for the user-space gateway
- `scripts/issue_tesla_gateway_cert_alpn.sh`: requests and installs the Let's Encrypt cert via TLS-ALPN on the configured local HTTPS gateway port
- `apache/tesla-partner.conf.example`: example Apache vhost that serves only the Tesla partner `/.well-known/` public key (no dashboard proxy)
- `mysql/tesla_energy_history.sql`: Tesla MySQL schema for live samples and imported history
- `templates/dashboard_status.html`: live dashboard UI, Tesla energy chart, and controller-trace debug chart
- `templates/charger_profiles.html`: summer/winter charger profile settings UI
- `templates/system.html`: charger hardware, DB, Tesla, and HVAC system settings UI
- `templates/sessions.html`: session history UI
- `static/style.css`: shared styling
- `systemd/carcharger-automation.service`: automation service entry
- `systemd/carcharger-dashboard.service`: dashboard service entry
- `systemd/carcharger-tesla-public-gateway.service`: user-space Tesla public gateway entry

## Update Rules

When implementing future changes in this repo:

- Read this file first
- Use `README.md` for operator/setup steps
- Use `config.yaml` before making assumptions about live behavior, because local automation settings override code defaults
- Use service logs before changing logic when the user reports that something already happened on the running system
- Only re-scan source files that are directly affected by the requested change
- Update this file whenever app behavior, routes, config shape, dependencies, or operating assumptions change
- If code and this file disagree, treat the code as current truth and then bring this file back in sync

## Limits Of This File

This file is a working project brief, not a substitute for source validation.

It should be enough to start most changes quickly, but source still needs to be checked for:

- exact function names and signatures
- edge-case behavior
- template structure
- current config keys if a requested change touches them
