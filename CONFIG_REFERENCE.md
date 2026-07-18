# Carcharger Config Reference

This is the operator-facing guide for the settings that most affect charging behavior. The main editing path is the `/charger` web UI, which shows these explanations beside the fields. Use `config.yaml` for the live values and `config.example.yaml` for a commented sample.

## Where The Charging Settings Live

The active seasonal profile is selected by:

```yaml
automation:
  active_profile: summer
```

The live automation settings are then read from:

```yaml
automation:
  profiles:
    summer:
      ...
      energy_controller:
        ...
```

On this machine, `summer` is currently the active profile.

## Main Control Ideas

### Charger Socket Handling

The charger integration defaults to short-lived Tuya sockets:

```yaml
charger:
  socket_persistent: false
  socket_persistent_recycle_seconds: 900
  updatedps_refresh: false
```

Meaning:

- Each charger status read or command closes its local Tuya socket afterward.
- With `socket_persistent: true`, one TCP session is reused and recycled every `socket_persistent_recycle_seconds`. **Do not set the recycle short** (e.g. 120s): some Tuya v3.4 firmwares wedge their local daemon after a few thousand accepted TCP sessions — they keep answering ping and cloud/app traffic but silently drop the local key handshake, which tinytuya reports as error `914` ("Check device key or version"), and only a physical power cycle recovers them (see [tinytuya #581](https://github.com/jasonacox/tinytuya/issues/581)). This wedged the Aimiler on 2026-07-04 after ~48h of 120s recycling; 900s ran for weeks without issue.
- Some charger firmwares reply with cached DPS values for the whole life of a persistent session. `updatedps_refresh: true` sends UPDATEDPS (command 18) before each status poll so DP values stay fresh in-band, instead of relying on frequent reconnects to get fresh data.
- A persistent `914` after days of working is almost always the device-side wedge above, not a real key change.
- If charger reads fail, the controller recreates the charger connection and retries once before showing `CONTROL_UNAVAILABLE` on the dashboard.

### Fetch Error Backoff

```yaml
automation:
  fetch_error_backoff_after_failures: 6
  fetch_error_backoff_max_seconds: 600
```

Meaning:

- When consecutive charger status fetches keep failing, the control-loop sleep stretches exponentially (30s → 60s → … capped at `fetch_error_backoff_max_seconds`) instead of hammering a wedged charger with handshake attempts every cycle. Each failing cycle records two errors, so the default `6` starts backing off after 3 failed cycles.
- The backoff clears on the first successful cycle. Set `fetch_error_backoff_max_seconds: 0` to disable.

### Raw Tuya Response Debugging

Raw local Tuya status and command responses can be collected for troubleshooting:

```yaml
charger:
  debug_tuya_responses: false
```

Meaning:

- The `/system` page exposes this as **Debug Tuya Responses**.
- When enabled, raw Tuya responses are appended to `tuya_raw_debug.jsonl` or the configured `raw_tuya_log_path`.
- The automation service reloads `config.yaml`, so changing the switch and saving System settings is picked up by the next control loop.

### Charger Telemetry Logging

When MySQL logging is enabled, charger troubleshooting samples are stored in:

```yaml
database:
  telemetry_table: charger_telemetry_samples
```

Meaning:

- Each automation cycle records the raw charger DPS JSON when the database is available.
- DP `6` is also stored as its raw base64 message and decoded into observed voltage/current/power fields.
- The DP `6` decoder is based on Aimiler UA_7KW observations, not an official vendor spec, so these fields are for troubleshooting and future logic refinement.
- Tuya Cloud identifies DP `10` as the fault bitmap, DP `33` as `mode_set`, and
  DP `108` as `charge_record`. The controller records all of them, but only
  non-zero DP `10` is treated as a raw-DP fault trigger.

### Pre-4PM Solar Margin Caps

Before 4PM, these fields are a deadband over `solar generation - home consumption`:

```yaml
solar_buffer_min_watts: 200
solar_buffer_max_watts: 500
```

Meaning:

- `solar_buffer_min_watts`: if margin falls below this, lower EV amps by about half of the excess amps, rounded up, with at least a 1A decrease; stop at minimum when minimum charging no longer fits.
- `solar_buffer_max_watts`: if margin rises above this, raise EV amps by about half of the remaining available amp headroom, rounded up, with at least a 1A increase.
- Between min and max, hold current EV amps.
- The max must be at least `300W` above the min to avoid rapid up/down chatter.

Before 4PM, Powerwall SOC does not change this math.

### Legacy Gap Deadband

These fields are retained for compatibility, but pre-4PM solar-follow uses the
min/max caps above:

```yaml
solar_gap_boost_watts: 500
solar_gap_trim_watts: 200
```

Meaning:

- Raise EV amps only when at least `500W` is free above the current setpoint.
- Lower EV amps by at least half of the required correction when the remaining gap falls below `200W`, bounded by the ramp-down limit.
- Hold current amps between those two values.

These are no longer shown in the main `/charger` form because they are not the
active daytime solar-follow knobs.

### Down-Hold

This field delays downward amp changes and low-solar stops:

```yaml
energy_down_sustain_seconds: 180
```

Meaning:

- A coffee-maker-style load must persist for about 3 minutes before the charger is reduced.
- Safety stops still bypass this hold, including no-charge window, emergency low Powerwall SOC, and confirmed missing required integrations.

### Ramp Limits

These fields limit how fast amps can move after the decision is made:

```yaml
ramp_up_amps_per_loop: 2
ramp_down_amps_per_loop: 4
```

Meaning:

- Even if solar improves a lot, the target rises by at most 2A per decision loop.
- If solar/load worsens and the hold allows it, the target falls by at most 4A per decision loop.

### Charger Command Cooldown

This field limits how often the controller sends commands to the Aimiler charger:

```yaml
charger_command_min_interval_seconds: 180
```

Meaning:

- The decision can update every loop, but hardware commands are paced.
- This prevents rapid start/stop or amp command chatter.

### Low-Solar Grace

This field is the pure energy-controller grace before a low-solar stop:

```yaml
low_solar_stop_grace_loop_count: 2
```

Meaning:

- If solar is below minimum and cloudy fallback does not apply, the decision engine waits this many loops before asking to stop.
- The controller-side `energy_down_sustain_seconds` can then hold that low-solar stop longer while the charger is already active.

## Tesla Freshness

These fields decide when Tesla telemetry is too old:

```yaml
tesla_stale_after_seconds: 600
tesla_critical_stale_after_seconds: 1800
critical_stale_action: stop
```

Meaning:

- After 10 minutes, telemetry is stale but still usable with caution.
- After 30 minutes, telemetry is critically stale.
- With `critical_stale_action: stop`, the charger stops when Tesla data is too old for safe solar-follow math.

## Time Windows

Important active windows:

```yaml
cloudy_day_force_charge_time: "11:00"
no_charge_start_time: "16:00"
night_charge_start_time: "21:00"
night_charge_end_time: "03:00"
night_charge_amps: 32
```

Meaning:

- After 11:00, cloudy fallback can allow exactly the minimum solar charging amps before the no-charge window.
- From 16:00 to 21:00, charging is blocked unless emergency mode or the full-Powerwall solar-follow exception applies.
- From 21:00 to 03:00, night charging targets 32A when the vehicle is connected.

## Quick Tuning Guide

For fewer small amp changes:

- Increase the distance between `solar_buffer_min_watts` and `solar_buffer_max_watts`.
- Increase `energy_down_sustain_seconds` if downward changes are too reactive.

For faster reaction to sustained house load:

- Lower `energy_down_sustain_seconds`.
- Increase `ramp_down_amps_per_loop`.

For more conservative Powerwall/solar protection:

- Increase `solar_buffer_min_watts` and `solar_buffer_max_watts`.
