# Energy Decision Algorithm

This document describes how `decide_energy_action()` in `energy_controller.py` computes each charging decision, the bugs fixed in May 2026, and known limitations with future improvement placeholders.

---

## Overview

The control loop runs every 60 seconds. Each iteration:

1. Reads the latest Tesla telemetry from MySQL (`tesla_energy_live_samples` — up to ~5.5 min old)
2. Reads the charger's current state from Tuya (live, polled every loop)
3. Calls `decide_energy_action()` — pure function, no side effects
4. Applies controller-side safeguards such as the down-hold timer
5. Executes the decision if the 180-second command cooldown has cleared

---

## Decision Priority (highest to lowest)

1. **Emergency charging mode** — fixed amps, bypasses everything
2. **Night charging window (21:00–03:00)** — fixed 32A when vehicle connected
3. **Night session continuing after 03:00** — if already charging, continue
4. **Emergency low-SOC stop** — hard stop if Powerwall below threshold
5. **Tesla telemetry missing / critically stale** — safety stop or min charge
6. **No-charge window (16:00–21:00)** — stop unless Powerwall is full and solar available
7. **Solar follow / protect mode** — main daytime logic (described below)

---

## Daytime Solar Follow Logic

### Step 1 — Before-4PM solar margin deadband

```
solar_margin = solar_watts - adjusted_consumption
```

Before 4PM, Powerwall SOC does not change solar-follow math. The UI min/max
solar buffer fields are a deadband over `solar - home`:

- Below `solar_buffer_min_watts`: lower EV charging by about half of the
  excess amps, rounded up, with a minimum 1A decrease; stop if already at
  minimum amps and minimum charging no longer fits
- Between min and max: hold the current EV setpoint
- Above `solar_buffer_max_watts`: raise EV charging by about half of the
  remaining available amp headroom, rounded up, with a minimum 1A increase

The config validator requires at least 300W between min and max to avoid chatter.

### Step 2 — Compute reference EV budget

```
allowed_total = max(0, solar_watts - solar_buffer_min_watts)
```

This reference budget is reported for debugging and start decisions, but the
active before-4PM regulation is the min/max margin deadband above.

### Step 3 — Estimate current EV consumption (temporal-consistency fix)

Tesla's `house_consumption_watts` is from a snapshot up to 5.5 min old. The Tuya charger reports `actual_power_w` at the current moment. Mixing these two values produces a systematic error:

- **Phase A (over-ramp):** If the EV ramped up after the Tesla snapshot, `power_w > house_from_Tesla` → `non_ev` clamps to 0 → system sees "no house load" → ramps to maximum
- **Phase B (phantom step-down):** If the EV setpoint was raised since the snapshot, `setpoint × V > power_w` → `non_ev` inflated → unnecessary step-down

**Fix:** Use the setpoint that was active at the Tesla snapshot time, not the current Tuya reading.

```python
ev_watts = _setpoint_at_time(charger.setpoint_history, telemetry.timestamp) * voltage
```

`setpoint_history` is a rolling list of `(timestamp, amps)` pairs appended on every `SET_AMPS` command (last 5 kept). On startup, the list is seeded with the current setpoint at `now - 1 hour` to ensure there is always a pre-snapshot entry available.

Fallback chain: `setpoint_at_snapshot → current_setpoint_amps → current_amps → ev_min_amps`.

When the charger has been stable for at least `charger_command_min_interval_seconds` after a `SET_AMPS` or `START` command, and the Tesla sample is not older than that command, the controller may use the charger reported actual watts when actual output is below the configured setpoint. This lets the controller recognize vehicle-side caps, such as a plug-in hybrid accepting only about 16A while the EVSE is offering more.

If Aimiler first reports active charging after the Tesla sample timestamp, the
controller is conservative unless there is evidence that the sample already
contains EV load. A controller START is tracked separately from later amp
changes, so a delayed Tuya active report still uses the setpoint history from
the original offered-output time. If output was offered and Tesla home load
rises by roughly the expected EV watts while Tuya is still reporting
inserted/0W, the charger can be treated as inferred active. Before 4PM, a
manual/non-command start after the 11:00 cloudy fallback time is treated as a
legitimate connected-vehicle event and kept at minimum amps unless a safety
stop condition applies.

### Step 4 — Compute non-EV house load

```
non_ev = max(0, adjusted_consumption - ev_watts)
```

`adjusted_consumption` = `house_from_tesla` + predictive loads (HVAC that changed after the snapshot).

### Step 5 — Available EV budget

```
available = allowed_total - non_ev
desired_amps = floor(available / ev_voltage)
```

### Step 6 — Apply before-4PM min/max cap

```
if solar_margin < solar_buffer_min_watts:
    target = current - ceil((current - available_amps) / 2), at least -1A
    # if available_amps is below minimum, step toward minimum, then stop there
elif solar_margin > solar_buffer_max_watts:
    target = current + ceil((available_amps - current) / 2), at least +1A
else:
    target = current
```

If the charger is off before 4PM, it starts at minimum amps only when starting
would still leave at least `solar_buffer_min_watts` of solar margin.
The same start-margin test is used when the charger appears to have started
externally after the latest Tesla sample.

When Aimiler status is incomplete or contradictory, the executor combines the
raw DPs with command history and Tesla home load. A payload missing only DP18
(switch state) is accepted as live degraded status if the observation DPs are
present. If a recent START is followed by a Tesla sample whose home load rises
by roughly the expected EV watts, the charger can be treated as inferred active
even if the switch DP is missing. Conversely, after a STOP, if Aimiler still
reports `charger_charging`/power but Tesla home load has dropped below an
EV-sized load, the active/power DPs are treated as stale post-stop settling.
When a later START occurs after a STOP, `ev_active_since` is moved to that
latest START so Tesla samples from before the START do not have EV load
subtracted.

Between `cloudy_day_force_charge_time` and the 16:00 no-charge window, the
pre-4PM branch applies cloudy fallback before returning a low-solar stop. This
keeps a connected or already-offered vehicle at the configured minimum amps
(default 8A) on cloudy days, while still allowing safety stops, zero-solar
stops, no-charge-window behavior, critically stale telemetry handling, and
confirmed integration failures to override it.

After the pure energy decision returns, `solar_ev_controller.py` applies a controller-side down-hold. If the decision would lower amps or stop for low solar while the charger is active, it holds the current target for `energy_down_sustain_seconds` before acting. The default is 180 seconds, so short appliance loads such as a coffee maker must persist for about 3 minutes before EV charging is reduced. Confirmed safety stops, emergency low-SOC, no-charge windows, confirmed missing integrations, known predictive loads, and externally started low-solar sessions bypass this hold.

For low-solar stops, the low-solar grace counter is preserved while the
down-hold is pending, so the hold expires once instead of restarting grace every
loop. If a STOP has already been sent and the charger switch is disabled, the
down-hold does not re-enable output just because Aimiler still reports
`active=True`; that state is treated as charger/vehicle settling.

---

## Predictive Load Adjustments

When an HVAC state changes after the last Tesla snapshot, a predictive adjustment is applied to `adjusted_consumption` so the system reacts before Tesla catches up.

**HVAC cooling:** Adds `hvac_load_watts` (3600W default).
**HVAC heating:** Adds 500W.

Known appliance starts and stops bypass the slow solar deadband/ramp and the 3-minute down-hold. The hold is for unexplained home-load changes, such as a brief coffee maker load. If HVAC explicitly reports on or off, the controller treats that as known state and immediately reserves or releases that load against stale Tesla telemetry. An HVAC off transition subtracts the configured load estimate until Tesla catches up, because the stale Tesla sample may still include the old appliance load.

Predictive timestamps are cleared when a Tesla snapshot arrives that post-dates the state change (with a 30-second tolerance window to absorb near-simultaneous events).

---

## Grace Period — Stop Pending Fix

When `desired_amps < 8A` and no cloudy fallback applies, the system enters a grace period before stopping. The charger takes 60–180 seconds to actually stop after a STOP command.

**Bug (fixed):** After sending STOP, the charger stays `is_charging=True` for 1–3 more loops. Each loop saw `last_decision=STOP → next_counter=0 → grace restarts from 1`, causing 3× stop attempts and the grace period to extend well beyond its intended 2 loops.

**Fix:** The grace period is bypassed when `last_charger_command_type == "STOP"` and `cooldown_remaining > 0`. The system holds at LOW_SOLAR_STOP and waits for the charger to confirm stopped.

---

## Tesla Polling

The `tesla_energy_sync.py` worker manages all Tesla API calls.

- **Active window:** 6am–9pm local time (`sync_active_start_hour / sync_active_end_hour` in config). No API calls outside this window.
- **Spacing:** Budget spacing is calculated against remaining solar hours in the month, not total hours. With 4,950 usable requests and 15 solar hours/day, this gives ~5.5–6 minute intervals during solar hours.
- **Budget:** `monthly_request_budget: 5000`, `monthly_request_reserve: 50` → 4,950 usable. At 6-minute intervals for 31 days: 4,650 requests, leaving ~300 surplus for spike confirmations.

### Spike Confirmation (non-EV load spikes)

When new Tesla telemetry arrives and unexplained non-EV load increased by >500W, the automation controller writes `tesla_refresh_request.json` with `requested_at = now + 2 minutes`. "Unexplained" means HVAC is not reporting active, so the spike is more likely a short appliance load. The sync worker wakes for due refresh requests instead of sleeping through the normal budget-spaced interval.

**Purpose:** A brief appliance load can clear before the confirmation pull. The fresh reading shows normal consumption and no extra step-down is needed. A sustained unexplained load is still present at confirmation time, so the down-hold can expire and the lower setpoint becomes warranted.

This uses one extra request per spike event. The 300-request monthly surplus covers up to ~9 spikes/day before hitting the budget limit.

### Future: Solar Drop Confirmation *(placeholder)*

**Not yet implemented.** On partly cloudy days with solar oscillating every 3–8 minutes, a Tesla snapshot landing during a cloud pass triggers an unnecessary step-down. By the time the next normal snapshot arrives (5.5 min), the sun may be back and the system ramps back up — causing repeated oscillation.

**Proposed mechanism:** Mirror the spike confirmation logic for solar drops. When new telemetry arrives and `solar_watts` dropped by >1500W compared to the prior snapshot, write a `tesla_refresh_request.json` for 3 minutes later. If the sun returned in that 3 minutes, the confirmation pull shows good solar and no step-down is issued.

**When to implement:** Next time a partly cloudy day produces visible oscillation in the trace logs. Check `energy_decision_trace.jsonl` for sequences of `POWERWALL_PROTECT → LOW_SOLAR_GRACE → POWERWALL_PROTECT` repeating every 5–6 minutes as the signal that this optimization is needed.

**Implementation point:** `solar_ev_controller.py`, in the spike detection block after `state_after["control"]["last_non_ev_watts"]` is updated. Add a parallel check: `if new_telem and prev_solar - curr_solar > 1500: write refresh request`.

---

## Charger Device Cache

The Tuya charger sometimes returns partial DPS responses (not all fields). Missing fields are filled from a local cache file (`charger_status_cache.json`).

Cache max age: **70 seconds** (slightly more than one 60-second loop). This ensures merged data is never more than one loop cycle old. The old default was 180 seconds.

---

## Config Fields Reference

| Field | Effect |
|---|---|
| `solar_buffer_watts` | Buffer fallback when SOC is unavailable |
| `solar_buffer_min_watts` | Buffer at full SOC (most permissive) |
| `solar_buffer_max_watts` | Buffer at empty SOC (most conservative) |
| `solar_gap_boost_watts` | Extra headroom required before raising EV amps |
| `solar_gap_trim_watts` | Remaining headroom threshold below which EV amps trim down by at least half of the required amp correction, bounded by `ramp_down_amps_per_loop` |
| `pre_4pm_powerwall_protect_soc` | SOC threshold that activates POWERWALL_PROTECT mode name (math is same either way) |
| `energy_down_sustain_seconds` | Controller-side hold before applying lower amps or low-solar stop |
| `ramp_up_amps_per_loop` | Max amps increase per decision loop (default 2A) |
| `ramp_down_amps_per_loop` | Max amps decrease per decision loop (default 4A) |
| `low_solar_stop_grace_loop_count` | Grace loops before stop when solar insufficient (default 2) |
| `charger_command_min_interval_seconds` | Cooldown between charger commands (min 180s) |
| `tesla_stale_after_seconds` | Age where Tesla telemetry is marked stale but can still be used cautiously |
| `tesla_critical_stale_after_seconds` | Age where Tesla telemetry is treated as unsafe for normal solar math |
| `critical_stale_action` | What to do when Tesla telemetry is critically stale (`stop` by default) |
| `enable_cloudy_day_fallback` | Force 8A minimum after `cloudy_day_force_charge_time` even without sufficient solar |
| `cloudy_day_force_charge_time` | Time after which cloudy fallback activates (default 11:00) |
| `sync_active_start_hour` | Local hour to start Tesla polling (default 6) |
| `sync_active_end_hour` | Local hour to stop Tesla polling (default 21) |
| `monthly_request_budget` | Total Tesla API calls allowed per month |
| `monthly_request_reserve` | Budget held back as reserve (not counted as usable) |

### Removed fields (silently dropped from old configs)

- `pre_4pm_consumption_cap_pct` — replaced by watt-based dynamic buffer
- `after_4pm_full_powerwall_generation_pct` — was never used in any decision
