# Kotiakku leftover + go-e smart charge

Home Assistant custom integration for one to four go-e Gemini chargers behind an Elisa Kotiakku (Huawei hybrid + LUNA). It writes charger `lot` / `amp` / `psm` / `frc` (`fup` stays false) from Kotiakku leftover solar, and can force full power on a charger during cheap charge windows.

Deploy from this GitHub repo: [§3](#3-deploy-in-home-assistant) (HACS custom repository, manual copy, or git). There are no YAML surplus/charge automations here.

This is the only drop. Do not also run the old YAML surplus or charge automations — they would fight this.

The go-e Controller is **read-only**. Never publish to `go-eController/…`. Never `frc=1`, `ama`, `loe`, `loty`, `lop`.

## 1. App and MQTT

- Chargers on `go-eCharger/<serial>/…`, **writes allowed** (`mcr=false`).
- Load balancing **on** (`loe=true`). Group total **50 A**. Charger max **32 A**. App charger priorities (`lop`) still apply inside go-e for the 50 A group. Leftover *who gets surplus watts* uses HA `number.kotiakku_goe_direct_priority_<serial>` (1 is highest, 99 is lowest). Do not write app `lop` from HA.
- MQTT in Home Assistant must be able to publish to `go-eCharger/<serial>/<key>/set`.

## 2. Sensors you pick

The integration does not assume entity ids. **Add integration** and **Configure** are entity pickers: charger 1 (required) plus optional chargers 2–4, the Controller Car-power mean, Kotiakku SoC / solar / house, the spot-price sensor, and optional solar **energy** forecast (kWh). Nothing is wired until you select it.

| Role | What to pick |
| --- | --- |
| LUNA SoC | Battery state of charge |
| PV power | Solar production |
| House power | House load **including** EV |
| Controller Car-power 5-min mean | go-e Controller EV watts for leftover math. Unknown → **0 W**. Do **not** keep last sample. Never written to. |
| Charger entities | Charger 1 is required; 2–4 are optional. Any entity on each charger device (car state is ideal), then that charger’s MQTT serial and leftover priority |
| Spot-price sensor | Needs `raw_today` / `raw_tomorrow` (HACS Nordpool) |
| Solar remaining today | Optional. Forecast.Solar / Solcast remaining-today kWh. Off-sun / Supercheap |
| Solar tomorrow | Optional. Forecast.Solar / Solcast tomorrow kWh. Off-sun / Supercheap |

Same form: whether Kotiakku solar/house are in kW (default on) and whether the Controller mean is in kW (default off = watts).

Leftover:

```
available_w = |solar_w| − |house_w| + |ev_w|
```

Solar is generation, house is consumption (including the cars). EV watts come from the **Controller** Car-power mean, not a charger entity. Use the magnitude of solar, house, and EV (an inverted CT can make Car negative). **Do not abs `available_w`.** A negative leftover is a deficit.

If the Controller mean is `unknown` (typical when nothing is charging), EV is **0 W**. House then has no car in it, so leftover is solar − house.

Unknown SoC, solar, or house → treat as a blocked window. Decisions wait `settle_s` (default 5 s) after those sensors stop changing, so one Gridle burst is one run.

The official Controller API has no combined-power key. Combined current is the charger key **`lot`**. **Who actually charges** is go-e load balancing (app `lop` still applies to the 50 A group). **Which surplus charger is offered leftover watts** is the HA leftover priority on each charger (`number.kotiakku_goe_direct_priority_<serial>`; 1 is highest, 99 is lowest). HA does not write `lop`, `loe`, or `loty`. It does not read MQTT `lop`.

A single charger always gets the leftover. When every listed charger is surplus **and HA leftover priorities are equal**, the same leftover `lot` / `psm` / `amp` / `frc` is written to all of them. That leftover `lot` is the group total. go-e load balancing splits it.

If HA leftover priorities **differ**, the higher-priority plugged surplus charger is offered what it is taking. Unused leftover above 500 W (`number.kotiakku_goe_direct_remainder_floor_w`) goes to the next car. If that remainder is **below 3 kW** (`number.kotiakku_goe_direct_next_surplus_min_w`), HA cuts the high-priority share so the next car still gets 3 kW — only if the first still meets 6 A after the cut. Example: leftover 12 kW, high taking 10 kW → **9 kW + 3 kW**. Remainder at or below 500 W is a dead zone: do not *start* the next car. If the next car was already on and leftover then shrinks so the first would use it all, keep that 3 kW steal for the same 15 min hold (`number.kotiakku_goe_direct_hold_minutes`), then drop it. Group `lot` starts from the **total** leftover and is raised if needed so both per-charger `amp` caps fit (still at most group lot 50). Each charger’s `psm` / `amp` comes from **its** allocation. HA reads per-charger `nrg` (MQTT or `sensor.go_echarger_<serial>_nrg`) and never writes `lop`. Slot defaults are charger 1 → 1, charger 2 → 2, and so on (charger 1 highest). Set equal numbers to share leftover the way load balancing does.

When one charger is full-power (`lot` 50 / `amp` 32), surplus does **not** write a smaller leftover `lot` to the other charger — last writer would shrink the group to leftover amps and both cars would be stuck at that cap, so app priorities could not give 32 A to the cheap-hour session. Surplus keeps group `lot` at 50 and sets leftover `amp` / `psm` / `frc` on the surplus charger only. That leftover `amp` is the surplus energy cap, not an HA ranking. Combined demand may exceed 50 A (`32` + leftover `amp`); **app priorities then split the 50 A group.** HA does not reserve 32 A for the cheap-hour charger by capping surplus `amp`.

`amp` cannot be 0 (official range 6–32). Stopping surplus-style charging is **`frc=0`** (neutral / ECO), not `amp=0` and not **`frc=1`** (force off would block cheap-hour ECO). `amp`/`psm` alone do not start a session in ECO on expensive hours; **`frc=2`** does.

Budget from leftover watts (floored amps, Finnish 230 V):

1. If leftover ≥ 3-phase leftover (default 4140 W = 6 A × 230 V × 3) → `psm=2`, `lot = leftover // (230 × 3)`
2. Else → `psm=1`, `lot = leftover // 230`
3. `lot` is at least 6 A and at most group lot (default **50 A**). Per-charger `amp = min(max amp, lot)` (default **32**). 3-phase amp is leftover ÷ (230 × 3), not stuck at 6 A. If a car is already at the published amp cap and leftover would allow more, it is offered leftover again so amp can rise.
4. A published `psm` change (1-phase ↔ 3-phase) waits the same hold minutes (default 15) **both up and down**. CCS cannot switch phases in-session: go-e pauses charging for several seconds, and Tesla can send a charging-stopped / interrupted app alert (often CP_a055). Holding `psm` does **not** freeze amp: leftover is still budgeted on the phase that is actually running. 1→3: 1-phase leftover, capped at max amp (8 kW → 32 A, not the pending 11 A 3-phase). 3→1: 3-phase min amp (6 A), not the pending 1-phase amp. The first surplus start has no last `psm`, so it picks 1- or 3-phase immediately. Full-power force-on writes `psm=2` immediately.

Start still needs SoC ≥ surplus SoC on (default **92%**) and leftover ≥ start leftover (default **2000 W**). After that, do **not** cut to zero every time leftover dips. While the session is on:

- Leftover ≥ low hold leftover (default **1000 W**) and SoC ≥ SoC on minus hysteresis (default **90%**) → budget tracks leftover
- Leftover below 1000 W, **or** SoC below 90%, **or** Kotiakku SoC / solar / house unknown or unusable → keep **6 A** for up to the low hold minutes (default 15). If the last surplus `psm` was 3-phase, stay 3-phase 6 A for that window so Tesla is not interrupted twice (phase switch, then stop)
- Leftover wants the other phase → keep the current `psm` for those same 15 min, then switch. Leftover returning to the current phase cancels the timer
- Second surplus charger already on, leftover then shrinks so the high-priority car would use it all → keep the 3 kW second-car floor for those same 15 min, then drop it
- That low hold for the whole duration → `frc=0` and restore ECO (`psm` Auto, `amp` 32, `lot` 50)
- Recovered leftover, SoC, or sensors cancel the hold timer. A warning is logged when Kotiakku values are unusable. Surplus cannot **start** while those sensors are unusable. Restart loses remaining hold minutes (same as the other holds).

MQTT order: start is `fup` / `psm` / `lot` / `amp` then `frc=2`; stop is `frc=0` first.

## 3. Deploy in Home Assistant

This GitHub repo is the source. Home Assistant loads the integration from **`<config>/custom_components/kotiakku_goe_direct/`**. `<config>` is the directory that also holds `configuration.yaml` (HAOS: `/config`, or the Samba `config` share).

Do **not** copy the whole repo into `custom_components/`. Only the inner `kotiakku_goe_direct` folder belongs there. Do not also run the old YAML surplus or charge automations.

### Prerequisites

- MQTT in Home Assistant can **publish** to `go-eCharger/<serial>/<key>/set`. This integration depends on the MQTT integration.
- Chargers: MQTT writes allowed (`mcr=false`), load balancing on (`loe=true`), group total 50 A, charger max 32 A. Set leftover priority on the HA device (`number.kotiakku_goe_direct_priority_<serial>`). App `lop` still applies to the 50 A group.
- Sensors you will pick already exist: Nordpool (with `raw_today` / `raw_tomorrow`), go-e Controller Car-power 5-min mean, Kotiakku SoC / solar / house, charger entities. Optional: solar remaining-today and tomorrow kWh.
- The go-e Controller is read-only. Never publish to `go-eController/…`.

### A. HACS custom repository (recommended)

The integration is not in the HACS default store. Add this repo as a custom repository, then download it.

1. Install [HACS](https://www.hacs.xyz/docs/use/download/download/) if it is not already there, and finish **Settings → Devices & services → Add integration → HACS**.
2. Open **HACS**.
3. Top-right **⋮ → Custom repositories**.
4. Repository: `https://github.com/kkhalme/kotiakku_goe_direct`
5. Type: **Integration** → **Add**.
6. Search **Kotiakku go-e Direct** → **Download**.
7. **Restart Home Assistant**.
8. **Settings → Devices & services → Add integration → Kotiakku go-e Direct**. Pick the price sensor, charger 1 (required) and optional chargers 2–4, each charger’s **MQTT serial** (pre-filled when a guess is confident) and leftover priority, then Controller / Kotiakku sensors.

Later updates: HACS shows a pending update; download it and restart.

### B. Manual copy (no HACS)

1. Copy [`custom_components/kotiakku_goe_direct`](custom_components/kotiakku_goe_direct) to `<config>/custom_components/kotiakku_goe_direct/`.
   - HAOS: Samba `config` share, the File editor add-on, or the SSH add-on.
   - Container / Core: the same path you mount as `/config`.
2. Restart Home Assistant.
3. Add the integration as in A.8.

Replace the folder on updates, then restart. Do not leave a second copy under another name.

### C. Git on the Home Assistant host (easy `git pull`)

SSH into the machine that has `<config>`. Do **not** clone the repo *as* `custom_components/kotiakku_goe_direct` — that would nest the files wrong.

```bash
cd /config
mkdir -p custom_components
git clone https://github.com/kkhalme/kotiakku_goe_direct.git /config/kotiakku_goe_direct
ln -sfn /config/kotiakku_goe_direct/custom_components/kotiakku_goe_direct \
        /config/custom_components/kotiakku_goe_direct
```

Restart, then add the integration as in A.8. Update with `git -C /config/kotiakku_goe_direct pull` and restart.

### After it is installed

Policy pickers start at **Force off**. Surplus can run; no 22 kW grid charge until you pick a policy.

After it exists, **Configure** edits charger entities/serials/priorities and Controller / Kotiakku wiring. Surplus numbers, ECO phase, window bounds, the price text, leftover priorities, and policies are entities on the **Kotiakku go-e Direct** device so they can go on a dashboard.

YAML import is optional. The ids below are **placeholders** — use your own entities and the MQTT serials from the go-e app (`111111` / `222222` are fake). Charger 1 is required; chargers 2–4 may be omitted. `priority` is 1–99 (1 is highest); omit it to default by slot (1, 2, 3, 4):

```yaml
kotiakku_goe_direct:
  price_entity: sensor.nordpool_kwh_fi
  controller_entity: sensor.go_econtroller_ev_power_5_min_mean
  controller_in_kw: false
  soc_entity: sensor.battery_soc
  solar_entity: sensor.solar_power
  house_entity: sensor.house_power
  kotiakku_in_kw: true
  solar_remaining_entity: sensor.energy_production_today_remaining
  solar_tomorrow_entity: sensor.energy_production_tomorrow
  chargers:
    - entity: sensor.go_echarger_111111_car_state
      serial: "111111"
      priority: 1
    - entity: sensor.go_echarger_222222_car_state
      serial: "222222"
      priority: 2
```

The two **kW** checkboxes are not day-to-day knobs. They say whether the picked power sensors report kilowatts or watts so leftover math can convert to watts. Kotiakku solar/house default to kW; the Controller Car-power mean defaults to watts.

If **Kotiakku go-e Direct** is missing from Add integration, the files are not at `<config>/custom_components/kotiakku_goe_direct/manifest.json` (do not nest an extra `kotiakku_goe_direct` inside that folder). Restart once more. Check **Settings → System → Logs** for `kotiakku_goe_direct`. MQTT must already be configured.

Optional 48 h leftover / price graphs: [§7](#7-graphs). HACS does not copy those YAML files.

## 4. Charger serials

Writes go to `go-eCharger/<serial>/<key>/set`. The serial is the MQTT path. It is **not** the entity id.

Guessing the serial from `sensor.go_echarger_<serial>_…` alone is not robust: entity ids can be renamed, and firmware MQTT vs HACS `goecharger-mqtt` use different unique_id shapes. The form therefore **asks for the serial** and only pre-fills when something more stable agrees:

1. State attributes `sse` / `serial` / `serial_number`
2. Device registry serial and identifiers (`go-e_<serial>`, `(goecharger_mqtt, <serial>)`, …)
3. MQTT topic `go-eCharger/<serial>/…` when HA exposes it
4. Entity `unique_id` (`go-e_<serial>_car_state` or `<serial>-sensor-car_state-…`)
5. Entity id / name, last resort (skipped for Controller entities)

Two high-confidence sources that disagree → no prefill; type it from the go-e app.

Car plug state: if you pick `*_car_state` (or unique_id `…_car`), that entity is used. Otherwise a sibling on the same device, then `sensor.go_echarger_<serial>_car_state`.

The Controller is only the leftover power sensor. It has no serial field and is never written to.

## 5. Use

Everything below is on the **Kotiakku go-e Direct** device (Settings → Devices).

Per charger, `select.kotiakku_goe_direct_policy_<serial>`:

- **Cheapest** / **Longest** / **Earliest** — that charger in that rank’s windows
- **Supercheap** — **Off-sun** windows, except no grid force-on when remaining-today **or** tomorrow solar energy is at least `number.kotiakku_goe_direct_solar_enough_kwh` (default **40 kWh**, `binary_sensor.kotiakku_goe_direct_solar_enough` on). Off-sun is cheapest 2–5 h after dropping hours whose expected energy is at least `number.kotiakku_goe_direct_offsun_hour_kwh` (default **1 kWh**). Remaining-today and tomorrow daily kWh are spread across local hours by solar elevation. Hours under 1 kWh (night, dawn, dusk, winter) stay Off-sun. Transfer + tax make even a cheap spot more expensive than leftover solar when the day is above 40 kWh, so Supercheap skips 22 kW then **including Off-sun night hours**. Surplus can still write that charger. Unknown forecast excludes nothing and is not enough solar. Cheapest / Longest / Earliest ignore the forecast.
- **Force on** — that charger full power now
- **Force on until unplug** — that charger until **its** car is Idle, then restore the previous policy
- **Force off** — surplus / ECO only

While a charger’s full-power policy is on, surplus skips **that** serial only. Spot-price windows and force-on do **not** read Kotiakku SoC / solar / house. Gridle going unknown only affects leftover surplus. HA still does not write app charger priorities (`lop`).

Windows are planned from `raw_today` / `raw_tomorrow` (or `today` / `tomorrow`). A valid window is a contiguous block whose duration is in [min, max] (default 2–5 h, step 0.25) and whose **every** 15-minute slot is at or below the price ceiling (default 0.1, native unit of the price sensor). Four independent greedy disjoint plans (cap 16): cheapest, longest, earliest, and off-sun. Off-sun is the cheapest ranking after hours with ≥ 1 kWh expected solar are dropped; gaps already split windows. A frozen window does not slide 15 minutes. Tomorrow’s curve typically appears sometime after 14:00 local. Off-sun also replans when remaining-today, tomorrow kWh, or the Off-sun hour knob change.

Full-power MQTT on that charger: `fup` false, `psm=2`, `amp=32`, `lot=50`, `frc=2`. After: `frc=0` first, then `psm` Auto, `amp=32`, `lot=50`.

| Entity | Default | Role |
| --- | --- | --- |
| `number.kotiakku_goe_direct_window_min_h` / `kotiakku_goe_direct_window_max_h` | 2–5 h | Window duration bounds |
| `number.kotiakku_goe_direct_electricity_price_ceiling` | 0.1 | 15-min electricity price cap (same unit as the price sensor) |
| `text.kotiakku_goe_direct_electricity_price_sensor` | from setup | Electricity price sensor id |
| `number.kotiakku_goe_direct_soc_on_pct` / `kotiakku_goe_direct_soc_hyst_pct` | 92 / 2 | Surplus SoC start (92%) and low-hold below 90% |
| `number.kotiakku_goe_direct_surplus_start_w` | 2000 W | Leftover to start surplus |
| `number.kotiakku_goe_direct_priority_<serial>` | slot 1–4 → 1–4 | Leftover offer order. 1 is highest, 99 is lowest. YAML/config seeds first add; then this entity. Equal values share leftover |
| `number.kotiakku_goe_direct_next_surplus_min_w` | 3000 W | Unequal HA leftover priority: next surplus floor. If unused leftover after the higher-priority car is below this (and above the remainder floor), steal this much from the first |
| `number.kotiakku_goe_direct_remainder_floor_w` | 500 W | Unequal HA leftover priority: remainder at or below this does not *start* the next car. Already-on next car keeps 3 kW for the hold minutes |
| `number.kotiakku_goe_direct_low_hold_w` | 1000 W | Leftover below this is the 6 A low hold |
| `number.kotiakku_goe_direct_settle_s` | 5 s | Wait after Gridle updates |
| `number.kotiakku_goe_direct_hold_minutes` | 15 min | Hold duration (leftover, SoC, second-car leftover gone, or 1↔3 `psm`) |
| `number.kotiakku_goe_direct_voltage_v` / `kotiakku_goe_direct_min_a` / `kotiakku_goe_direct_max_a` | 230 / 6 / 32 | Budget math |
| `number.kotiakku_goe_direct_phase3_min_w` | 4140 W | Switch leftover to 3-phase (after the 15 min `psm` hold) |
| `number.kotiakku_goe_direct_eco_lot_a` | 50 A | Group lot when restoring ECO |
| `select.kotiakku_goe_direct_eco_phase` | Auto | Restore `psm` (Auto / Force 1-phase / Force 3-phase) |
| `number.kotiakku_goe_direct_solar_enough_kwh` | 40 kWh | Supercheap: no 22 kW when remaining-today or tomorrow ≥ this, even in an Off-sun window. 0 disables. `binary_sensor.kotiakku_goe_direct_solar_enough` is that condition. `sensor.kotiakku_goe_direct_solar_kwh` is max(remaining-today, tomorrow) |
| `number.kotiakku_goe_direct_offsun_hour_kwh` | 1 kWh | Off-sun: drop a local hour when its expected forecast energy ≥ this. 0 disables. Dawn/dusk/night under 1 kWh stay Off-sun |

YAML `soc_on`, `eco_lot`, charger `priority`, … only seed those entities on first add. After that, change the device entities. If `number.kotiakku_goe_direct_next_surplus_min_w` still shows 11000 W from an older restore, set it to 3000 W.

## 6. Smoke checks

After the first surplus write, `go-eCharger/<serial>/lot/result`, `amp/result`, `psm/result`, `frc/result`, `fup/result` should be `true`.

| Expect | What you should see |
| --- | --- |
| SoC ≥ 92% and leftover ≥ 2000 W, both policies Force off, **equal** HA leftover priority | Same leftover `lot`/`psm`/`amp`/`frc` on both chargers. go-e shares |
| Only charger 1 configured | Leftover MQTT on that charger only. No second-car steal |
| SoC ≥ 92% and leftover 8 kW, **unequal** HA leftover priority, high at 6 A 3-phase | High is offered leftover (11 A), not locked at 6 A. Second car waits until high leaves unused leftover |
| SoC ≥ 92% and leftover 12 kW, high taking 10 kW | **9 kW + 3 kW**. Remainder 2 kW is above 500 W, so steal up to the 3 kW floor |
| Higher-priority car taking 7.5 kW of 8 kW leftover | Only the higher-priority charger: 500 W remainder is the dead zone (do not start the second car) |
| Second car already surplus-charging, leftover then drops so high would use it all | Keep **3 kW** on the second car for 15 min, then drop it |
| Higher-priority car Complete / WaitCar, leftover 8 kW | Lower-priority charger gets leftover if it still meets 6 A. WaitCar still gets an offer so it can start; no steal |
| Higher-priority car taking 10 kW of 18 kW leftover | Lower-priority charger gets the remaining 8 kW (already ≥ 3 kW, no steal) |
| Leftover 4 kW, unequal HA leftover priority, both wanting surplus | Only the higher-priority charger: it wants all 4 kW |
| Surplus on, leftover collapses below 1000 W | `lot` 6 for up to 15 min (stay 3-phase 6 A if that was the last `psm`), then `frc` 0, `psm` Auto, `amp` 32, `lot` 50 |
| Surplus on 1-phase, leftover rises to 8 kW | Stay `psm` 1, `amp` 32 for 15 min, then `psm` 2 / 11 A. Amp still tracks leftover while held |
| Surplus on 3-phase, leftover drops to 3 kW | Stay `psm` 2, `amp` 6 for 15 min, then `psm` 1 / 13 A |
| SoC 90–91% during a session | Keep tracking leftover. Not a hold, not a stop |
| SoC &lt; 90% | Same 6 A low hold as leftover &lt; 1000 W. Not `frc=1`, not an immediate cut |
| Kotiakku SoC / solar / house unknown or unusable | Warning in the log; same 6 A low hold on **Force off** chargers. Stop surplus only if still unusable after 15 min |
| Ranked policy + that rank binary on | **That** charger `psm` 2, `amp` 32, `lot` 50, `frc` 2 even if Kotiakku is unknown. Surplus skips **that** serial only; it does not lower group `lot`. Leftover `amp` on the other charger is not cut to leave 32 A; app `lop` splits the 50 A group |
| Supercheap, Off-sun window on, remaining-today and tomorrow under 40 kWh | **That** charger 22 kW |
| Supercheap, Off-sun or cheapest window on, remaining-today or tomorrow ≥ 40 kWh | No full-power; surplus may still write that charger. `binary_sensor.kotiakku_goe_direct_solar_enough` on |
| Supercheap, hour with ≥ 1 kWh expected solar | Not an Off-sun hour; no Supercheap 22 kW in that hour |
| Supercheap, remaining-today 8 kWh and tomorrow 6 kWh | Off-sun (hours under 1 kWh). Night cheap hours still 22 kW |
| Supercheap, forecast unknown or unset | Same as Cheapest (nothing excluded, not enough solar) |
| Cheapest, tomorrow 70 kWh | Still full-power. Forecast only shapes Off-sun / Supercheap |
| Force off during a price window | That charger’s full-power binary stays off; surplus may still write that charger |

## 7. Graphs

The 48 h Finnish year-round cases (spot, cheapest 2–5 h window, leftover, per-charger phase / amp / commanded kW) can be drawn. Default `--plot` is **Supercheap** (Off-sun hour 1 kWh, skip 22 kW when upcoming ≥ 40 kWh). Add `--cheapest` for the ungated Cheapest run:

```bash
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot --cheapest
```

Live Home Assistant, same series (HACS does **not** install these): copy [`homeassistant/packages/kotiakku_goe_direct_graph.yaml`](homeassistant/packages/kotiakku_goe_direct_graph.yaml) into `<config>/packages/` and replace the placeholder entity ids / fake serials `111111` and `222222`. Add [`homeassistant/dashboards/kotiakku_goe_direct_48h.yaml`](homeassistant/dashboards/kotiakku_goe_direct_48h.yaml) as a YAML dashboard. The first view uses [apexcharts-card](https://github.com/RomRider/apexcharts-card) so Nordpool `raw_today` / `raw_tomorrow` (including the 14:00 day-ahead curve) can share a 48 h-from-midnight axis with leftover and the chargers. Built-in `history-graph` is the second view; it can only show recorded past, not tomorrow's prices. These files are display-only — they do not write MQTT.

## Tests

```bash
python3 custom_components/kotiakku_goe_direct/tests/test_serial.py
python3 custom_components/kotiakku_goe_direct/tests/test_planner.py
python3 custom_components/kotiakku_goe_direct/tests/test_clock_roll.py
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot --cheapest
```
