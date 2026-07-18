# Aimiler EV Charger Utility

Canonical project context for future code changes lives in `AGENTS.md`. Keep that file updated when app behavior, routes, config structure, or integrations change.

This project now includes:
- `solar_ev_controller.py` for charger status, commands, the pure energy-decision controller, night charging, and emergency override.
- `web_config.py` for the status dashboard, Tesla chart, live energy-controller config APIs, charger profile settings, and system settings.
- `energy_controller.py` for pure daytime/night/emergency decision logic, validation, and preview support.
- `tesla_energy.py` for Tesla Fleet OAuth, Powerwall/site reads, and cached home-energy chart history.
- `tesla_local_auth.py` for one-time localhost Tesla OAuth bootstrap.
- `tesla_energy_sync.py` for budget-aware Tesla background syncing into MySQL.
- `mysql/tesla_energy_history.sql` for Tesla live-sample and imported-history tables.

## 1) Setup (always with venv)

```bash
cd ~/carcharger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Web dashboard and settings

Run the web app:

```bash
cd ~/carcharger
source .venv/bin/activate
python3 web_config.py
```

Open:
- `http://<your-pi-ip>:8788/dashboard` for live status, emergency charging, "charge N kWh" requests, and the Tesla energy chart with `1d`, `2d`, `3d`, `7d`, and custom date-time ranges
- `http://<your-pi-ip>:8788/charger` for summer/winter charger profile logic settings
- `http://<your-pi-ip>:8788/system` for Aimiler/Tuya hardware, HVAC endpoint, Tesla, and MySQL settings

Saved values are written to `config.yaml`.

### Dashboard password

The dashboard listens on all interfaces, so protect it with a password. Write
one to `.secrets/dashboard_password` (or set it on the `/system` page under
"Dashboard Access"):

```bash
mkdir -p ~/carcharger/.secrets
echo 'choose-a-password' > ~/carcharger/.secrets/dashboard_password
chmod 600 ~/carcharger/.secrets/dashboard_password
```

Every page and API then requires HTTP Basic Auth (any username, that
password). If the file is missing or empty, the dashboard runs unauthenticated
and logs a warning at startup.

### Charge a fixed amount of energy

Next to the emergency buttons the dashboard has a dropdown (1-20 kWh) and a
**Charge This Much** button. It starts an emergency-style charge and
automatically ends the override once the requested energy has been delivered
(measured from the charger's own power telemetry). A safety window of 1.5x
the expected charging time caps the override in case the session stalls.

After the target is reached, night-window auto-charging stays paused until
the car is unplugged, so plugging in during the night window delivers exactly
the requested energy instead of falling back into a full night charge.
Unplugging and replugging re-arms night charging, as does starting a new
emergency or energy request. Daytime solar charging is deliberately not
blocked: if there is surplus solar the next day, the car can still use it.
The same API is available directly:

```bash
curl -u user:PASSWORD -X POST http://<your-pi-ip>:8788/api/energy/emergency/start \
  -H 'Content-Type: application/json' -d '{"target_kwh": 10}'
```

## MySQL session logging

The automation can record charging sessions to MySQL, including:
- session start time
- session end time
- target/setpoint amps
- peak actual amps
- peak power
- total energy in Wh

Configure the `database:` block in `config.yaml` or in the `/system` settings page. The password is stored in a local secret file and the YAML only stores `password_file`. The automation uses the local `mysql` CLI, so no extra Python driver is required.

If your MySQL user is allowed to create the database/table, leave `bootstrap: true` and the controller will create them automatically. You can also create them manually with:

```bash
mysql --socket=/run/mysqld/mysqld.sock -u YOUR_USER -p < mysql/carcharger_sessions.sql
```

Open:
- `http://<your-pi-ip>:8788/sessions` for recent charging history and summary totals

## 3) Read charger status

```bash
cd ~/carcharger
source .venv/bin/activate
python3 solar_ev_controller.py --config config.yaml --status-only
```

## 4) Send charger commands

```bash
cd ~/carcharger
source .venv/bin/activate
python3 solar_ev_controller.py --config config.yaml --set-amps 16
python3 solar_ev_controller.py --config config.yaml --on
python3 solar_ev_controller.py --config config.yaml --off
```

## 5) Run the automation loop

This controller uses the configured seasonal profile, blocks EV charging between `16:00` and `21:00`, allows `32A` night charging from `21:00` until `04:00` when a vehicle is connected, and uses cached Tesla telemetry plus predictive HVAC load adjustments for daytime solar-following decisions.

Leave the charger in `Real-time` / `charge_now` mode, then run:

```bash
cd ~/carcharger
source .venv/bin/activate
python3 solar_ev_controller.py --config config.yaml --run-auto
```

To preview a single decision without changing the charger:

```bash
python3 solar_ev_controller.py --config config.yaml --run-auto --once --dry-run
```

To preview a hypothetical decision from JSON input:

```bash
python3 scripts/preview_energy_decision.py /path/to/preview.json
```

Emergency override from CLI:

```bash
python3 solar_ev_controller.py --emergency-on
python3 solar_ev_controller.py --emergency-off
```

The dashboard also has **Stop Charging To Unplug** for ordinary charging. It
stops output until the vehicle disconnects and automatically releases the hold
after one hour if the controller misses the unplugged interval.

## Current known settings from your device

- Tuya version: `3.4`
- Likely switch DP: `18`
- Likely current DP: `4`
- Likely mode DP: `14`
- Charger IP: configured in `config.yaml` (`charger.ip`)
- Charger hardware minimum current: `8A`
- Non-emergency automation safety minimum: `10A`
- Automated current changes are rate-limited to at least `180s` apart.
- Suspected charger fault/interlock events are logged to `charger_event_log.jsonl`.
- Charger status reads now retry automatically on partial Tuya payloads.
- Charger IP rediscovery runs once at automation service startup and again every 24 hours, matching by Tuya device ID and updating `config.yaml` if DHCP changes the IP.

## Auto-charge defaults

- When Tesla solar control is enabled for the active profile, daytime EV charging is no longer driven by fixed schedule slots.
- The controller now adjusts daytime amps from current Tesla live-sample surplus and grid-import margins instead of a preplanned time ramp.
- While the Nest HVAC API reports cooling, the EV charger stays on at the configured automation safety minimum, currently `10A`.
- `16:00-21:00` is a no-charge block unless emergency override is active.
- After `21:00`, the controller can start `32A` night charging whenever a connected vehicle is present.
- After `04:00`, it will not start night charging; it returns to daytime solar/cloudy-fallback logic.
- Emergency override charges at the configured maximum, currently `32A`.
- Daytime control uses cached Tesla telemetry, subtracts current EV load from Tesla house consumption, and can immediately subtract predictive HVAC load when it turns on after the last Tesla sample.
- A single configurable cooldown now applies to amp changes and charger start/stop commands.
- The controller uses a `30s` startup grace window before deciding that an enabled session has not started charging, then waits through `startup_failure_cooldown_seconds` before retrying.
- Current setpoint changes are rate-limited by `min_current_change_interval_seconds`; when a current change is made from an idle/off state, charging remains disabled during the settling interval instead of enabling immediately on the old reported setpoint.
- Suspected charger fault/interlock events are logged with raw DPS, and the controller makes one soft reset attempt per `fault_reset_cooldown_seconds` only when the charger state reports a fault/alarm or Tuya Cloud DP `10` reports a non-zero fault bitmap. DP `6` is decoded as observed voltage/current/power telemetry, DP `33` is `mode_set`, and DP `108` is `charge_record`; none of those raw payloads is a fault trigger by itself.
- When MySQL logging is enabled, each observed charging session is written to `charging_sessions`, and charger status troubleshooting samples are written to `charger_telemetry_samples` with raw DPS plus raw/decoded DP `6` telemetry.
- With `automation.log_fetch_details: true`, each automation poll logs the charger fetch duration and the exact charger state/power snapshot it read.
- The current default poll interval is `15s` to reduce stop-detection lag.

## Google Nest HVAC integration

If your local collector exposes `http://127.0.0.1:8789/api/hvac-status`, the controller will read it every poll using these automation settings:

```yaml
automation:
  hvac_status_url: "http://127.0.0.1:8789/api/hvac-status"
  hvac_timeout_seconds: 5
  allow_when_hvac_unavailable: true
```

When `is_running` and `is_cooling` are both `true`, the controller treats HVAC as an active shared load. Heating does not affect EV charging. When cooling is active, any non-emergency EV charging decision is reduced to the automation safety minimum. Emergency charging remains exempt from these shared-load limits. Set `allow_when_hvac_unavailable: false` only if you want missing HVAC data to block automated charging.
