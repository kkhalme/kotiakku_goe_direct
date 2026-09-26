# Kotiakku go-e Direct

Home Assistant integration for one to four go-e Gemini chargers behind an Elisa Kotiakku (Huawei hybrid + LUNA). It writes charger `psm` / `lot` / `amp` / `frc` over MQTT from Kotiakku leftover solar, and forces full power during cheap spot-price windows.

- Writes only `go-eCharger/<serial>/{fup,psm,lot,amp,frc}/set`. Never `go-eController/…`, never `ama`, `loe`, `loty` or `lop`.
- Leave chargers in Basic/default mode. Start is `frc=2`, stop is `frc=1`. Neutral (`frc=0`) is never used: in Basic/default it keeps charging.
- Do not also run the old YAML surplus or charge automations; they would fight this.

## Install

Requirements: Home Assistant 2025.1 or newer, the MQTT integration able to publish to `go-eCharger/<serial>/<key>/set`, chargers with MQTT writes allowed (`mcr=false`), load balancing on (`loe=true`), group fuse 50 A.

- **HACS:** ⋮ → Custom repositories → `https://github.com/kkhalme/kotiakku_goe_direct` (Integration) → download **Kotiakku go-e Direct** → restart.
- **Manual:** copy `custom_components/kotiakku_goe_direct` to `<config>/custom_components/` → restart.
- **Git:** clone the repo elsewhere under `<config>` and symlink `custom_components/kotiakku_goe_direct` into `<config>/custom_components/`.

Then **Settings → Devices & services → Add integration → Kotiakku go-e Direct**.

## Configuration

One form (also under **Configure**):

- **Spot-price sensor** with `raw_today` / `raw_tomorrow` (HACS Nordpool).
- **Kotiakku SoC, solar power, house power** (house includes the EV).
- **go-e Controller Car-power mean**, used to add EV watts back into leftover. Never written to.
- **Solar forecast today / tomorrow** (optional, full-day kWh such as Forecast.Solar `energy_production_today`).
- **Charger serials 1–4**: the MQTT path `go-eCharger/<serial>`, not an entity id. Charger 1 is required.

Power units come from each sensor's `unit_of_measurement` (W or kW). A sensor without a unit is read as W and logged once.

Car state and charging power come straight from each charger's MQTT topics (`car`, `nrg`). Check that they are retained: `mosquitto_sub -v -t 'go-eCharger/+/car'` should print a value immediately.

## Entities

All on the **Kotiakku go-e Direct** device.

Per charger (`<serial>`):

- `select.kotiakku_goe_direct_policy_<serial>`: **SolarPriority**, **SolarAndGrid**, **Force on**, **Force off** (default).
- `number.kotiakku_goe_direct_priority_<serial>`: leftover order, 1 is highest; ties go to the earlier charger slot.
- `switch.kotiakku_goe_direct_until_unplug_<serial>`: Force On Until Unplug, 22 kW until that car unplugs.
- `switch.kotiakku_goe_direct_after_charge_complete_keep_enable_<serial>` (default on) and `switch.kotiakku_goe_direct_after_charge_complete_keep_<serial>`: see *Keep* below.
- `sensor.kotiakku_goe_direct_role_<serial>`: `full`, `keep`, `surplus` or `off`, with the command, car, nrg, share and hold deadlines as attributes.

Shared:

- `sensor.kotiakku_goe_direct_window`: first planned window start; `windows`, `blocked`, `reason`, `tomorrow_ok`, `source_entity` attributes. `binary_sensor.kotiakku_goe_direct_window_active` is on inside a window.
- `binary_sensor.kotiakku_goe_direct_solar_enough`: SolarPriority skips 22 kW; attributes `gating_day`, `gating_kwh`, `today_kwh`, `tomorrow_kwh`, `usable_end`.
- `sensor.kotiakku_goe_direct_available_surplus`: held leftover still free for surplus chargers (W).
- Numbers (defaults): window min / max 2 / 5 h, price ceiling 0.2, flex 20 % / 0.02 €, SoC on 92 %, SoC hysteresis 2 %, surplus start 2000 W, hold 15 min, per-charger amp cap 32 A, max 1-phase amp 32 A, group lot 50 A, enough solar 40 kWh, off-sun hour 1 kWh, keep amp 6 A.
- Selects: keep phase (3-phase), surplus preferred start phase (1-phase).

## Behaviour

### Charger roles

Each charger has one role, first match wins:

1. **full** (22 kW: `psm=2`, `amp=32`, `lot=50`, `frc=2`): Force On Until Unplug, **Force on**, **SolarAndGrid** inside a window, or **SolarPriority** inside a window when solar is not enough.
2. **keep** (`psm`/`amp` from the keep knobs, `lot=50`, `frc=2`): the keep switch is on.
3. **surplus**: **SolarPriority** or **SolarAndGrid** outside full power.
4. **off** (`frc=1`): everything else, including **Force off**.

### Charge windows

- Price slots are today's and (after ~14:00) tomorrow's curve. Hours whose expected solar (the day's forecast kWh spread by sun elevation) is at least the off-sun hour threshold are removed from the search. A day without a forecast is not blocked.
- The window is the cheapest contiguous run of at least *window min* hours. If its average is above the ceiling there is no window. It then grows one slot at a time toward the cheaper neighbour while the average stays within the looser of flex % and flex € above the seed, no slot is above the ceiling, and the span stays within *window max*.
- If tomorrow's prices are in and that window does not overlap local today 22:00 through the end of tomorrow, a second window is seeded inside tomorrow.
- The plan depends on prices, forecasts and knobs, not on the clock: a window that has ended stays the plan until the inputs change.
- The search is a two-day price epoch: today + tomorrow once tomorrow's prices are in, otherwise yesterday + today from the stored cache. Midnight therefore does not drop a window that crosses midnight. When a new epoch arrives, a window that was already running is kept until it ends; one that had not started is dropped. `sensor.kotiakku_goe_direct_spot_price_history` exposes that cache (yesterday's average as the state).
- **Enough solar**: the gating day is today until tomorrow's prices are in and today's last hour with at least the off-sun threshold has ended; then tomorrow. Enough means that day's forecast is at least *enough solar*.

### Leftover surplus

- Leftover is `|solar| − |house| + |EV|`, where EV is the Controller mean (charger `nrg` if the Controller is unknown). EV is added back only when house is at least `EV − max(1000, EV/5)`, so a house CT that misses the charger does not invent surplus. Keep chargers' `nrg` is then subtracted.
- The leftover is sampled only when the Kotiakku SoC, solar or house value changes. Controller and `nrg` ticks do not move `amp`: following them made Tesla bounce between pilots. Kotiakku data older than 20 minutes (`last_reported`) or unusable counts as unusable.
- A charger starts when data is usable, its share is at least the start leftover (2000 W), and SoC is at least 92 % (or another charger is already on surplus). It keeps running at 1380 W (6 A) or more.
- Chargers are served in priority order. Each gets everything still unallocated; a charger that is actually taking (`nrg` ≥ 100 W) then consumes its `nrg` from the pool. A higher-priority car that is not taking stays armed, so it can start; a lower car already taking keeps its share until the higher car starts. Idle Complete (car Complete, `nrg` < 400 W) is skipped unless keep was cut.
- **Low hold**: a running charger whose share drops below 1380 W, SoC below 90 %, or unusable data keeps 6 A on its current phase for the hold minutes and then stops. Only a share at the start leftover cancels the hold.
- **Phase**: 3-phase stays while the share holds 6 A on three phases; 1-phase stays until 3-phase would deliver more watts (1-phase amp capped at max 1-phase amp). A first start uses the preferred start phase when both fit. Any phase change waits the hold minutes in both directions (CCS cannot switch phases in-session), while `amp` keeps tracking the share. A charger coming from 22 kW or keep is not a first start.
- `lot` is always the group fuse cap; surplus energy is each charger's `amp`. A leftover-sized `lot` lets go-e load balancing clip the car.

### Keep (after charge complete)

- Auto-on after 60 s of idle Complete when keep enable is on, the policy is not Force off, the charger is not a *steal victim* (another surplus charger with better priority is taking while surplus runs) and keep is not *cut*.
- *Cut* happens when HA stops charging while the car is Charging, WaitCar or Error, or when keep is switched off by hand while plugged. It clears when HA allows charging again before Complete, or on unplug. A cut charger is still offered leftover, so a car that raises its charge limit can leave Complete.
- Keep (and Force On Until Unplug) turn off when that car unplugs. go-e's Complete also appears after a forced stop, which is why cut and steal-victim exist.

### MQTT

- Every trigger waits 2 s for more triggers, then decides all chargers at once.
- A command is skipped when live go-e state already matches. When a known live value contradicts it (for example `frc=1` after an on), it is retried after 30 s; when values are just unknown, it is re-sent at most every 15 minutes.
- On: `fup=false`, `psm`, `lot`, `amp`, `frc=2`. Off: `frc=1`, `fup=false`.

## Smoke checks

| Situation | Expect |
| --- | --- |
| Both policies Force off, leftover 8 kW | `frc=1` on both |
| SolarPriority, car unplugged, leftover 6 kW, first start | `psm=1`, `amp=26`, `frc=2` (preferred 1-phase) |
| Running 1-phase, leftover rises to 8 kW | `psm=1`, `amp=32` for 15 min, then `psm=2`, `amp=11` |
| Running 3-phase, leftover drops to 3 kW | `psm=2`, `amp=6` for 15 min, then `psm=1`, `amp=13` |
| Leftover collapses below 1380 W | 6 A for 15 min, then `frc=1` |
| High priority taking 10 kW of 12 kW | Second charger gets 2 kW (`psm=1`, `amp=8`) |
| Cheap window ends at 3-phase, leftover 6 kW | Surplus continues on 3-phase (`amp=8`), no phase switch |
| SolarPriority window, enough solar | No 22 kW; surplus may run |
| Car Complete, `nrg` 0 | Not offered leftover; keep after 60 s |

## Tests

```bash
pip install -r requirements_test.txt
pytest
```

The core (`core/planner.py`, `core/engine.py`) has no Home Assistant imports, so the tests, including 48 h Finnish year-round simulations, run without Home Assistant.

## Graphs

`homeassistant/dashboards/kotiakku_goe_direct_48h.yaml` is a 48 h spot / window / leftover dashboard (needs HACS apexcharts-card). Edit the leftover sensor ids and the fake serials `111111` / `222222`.
