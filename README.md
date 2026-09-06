# Kotiakku leftover + go-e smart charge

Home Assistant custom integration for one to four go-e Gemini chargers behind an Elisa Kotiakku (Huawei hybrid + LUNA). It writes charger `lot` / `amp` / `psm` / `frc` (`fup` stays false) from Kotiakku leftover solar, and can force full power on a charger during cheap charge windows.

Deploy from this GitHub repo: [§3](#3-deploy-in-home-assistant) (HACS custom repository, manual copy, or git). There are no YAML surplus/charge automations here.

This is the only drop. Do not also run the old YAML surplus or charge automations — they would fight this.

The go-e Controller is **read-only**. Never publish to `go-eController/…`. Never `ama`, `loe`, `loty`, `lop`. Leave chargers in **Basic/default** charging mode; HA starts with **`frc=2`** and stops with **`frc=1`** (force off). Neutral (`frc=0`) is not used: in Basic/default it keeps charging. Do not use go-e Eco logic mode.

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
| Solar today | Optional. Forecast.Solar / Solcast **full-day** today kWh (example `sensor.energy_production_today`). SolarPriority. This is today's production, not leftover-from-now. |
| Solar tomorrow | Optional. Forecast.Solar / Solcast tomorrow kWh. SolarPriority |

Same form: whether Kotiakku solar/house are in kW (default on) and whether the Controller mean is in kW (default off = watts).

Leftover:

```
available_w = |solar_w| − |house_w| + |ev_w|
```

Solar is generation, house is consumption (including the cars). EV watts come from charger `nrg` when known, otherwise the **Controller** Car-power 5-min mean. Use the magnitude of solar, house, and EV (an inverted CT can make Car negative). **Do not abs `available_w`.** A negative leftover is a deficit.

Leftover is `solar − house + EV` only when house already contains the car. If house is clearly below the EV take (house CT misses the charger, or the Controller mean still includes a car that unplugged), EV is **not** added back — that would invent ~3 kW of surplus and keep charging from the grid.

If the Controller mean is `unknown` (typical when nothing is charging) and no charger `nrg` is known, EV is **0 W**. House then has no car in it, so leftover is solar − house.

Unknown SoC, solar, or house → treat as a blocked window. Decisions wait `settle_s` (default 5 s) after those sensors stop changing, so one Gridle burst is one run.

The official Controller API has no combined-power key. Combined current is the charger key **`lot`**. **Who actually charges** is go-e load balancing (app `lop` still applies to the 50 A group). **Which surplus charger is offered leftover watts** is the HA leftover priority on each charger (`number.kotiakku_goe_direct_priority_<serial>`; 1 is highest, 99 is lowest). HA does not write `lop`, `loe`, or `loty`. It does not read MQTT `lop`.

Leftover MQTT is **SolarPriority** and **SolarAndGrid** only. **Force off** never charges (`frc=1`), including leftover. Surplus on/off follows leftover watts and SoC, not plug-in: Idle or unplugged surplus chargers still get leftover `lot` / `psm` / `amp` / `frc=2` so **Allowd to charge** / Force State can go on before WaitCar. A finished car (`Complete`) is skipped. A single SolarPriority or SolarAndGrid charger always gets the leftover. When every listed surplus charger is surplus **and HA leftover priorities are equal**, the same leftover `lot` / `psm` / `amp` / `frc` is written to all of them (Idle included). That leftover `lot` is the group total. go-e load balancing splits it.

If HA leftover priorities **differ**, steal/take uses plugged cars only. Unplugged / Idle chargers are still armed with leftover MQTT; they cannot keep a 3 kW steal alive. Finished chargers are skipped: the next plugged car gets leftover as the first, not a 3 kW steal. The higher-priority plugged surplus charger is offered what it is taking. Unused leftover above 500 W (`number.kotiakku_goe_direct_remainder_floor_w`) goes to the next car. If that remainder is **below 3 kW** (`number.kotiakku_goe_direct_next_surplus_min_w`), HA cuts the high-priority share so the next car still gets 3 kW — only if leftover itself is at least **6 kW** (3 kW per car), the first is actually taking power, and both shares still meet 6 A. Example: leftover 12 kW, high taking 10 kW → **9 kW + 3 kW**. Leftover 6 kW → **3 kW + 3 kW**. Leftover 4.5 kW → no steal (would be 1.5+3). Remainder at or below 500 W is a dead zone: do not *start* the next car. If the next car was already on and leftover then shrinks so the first would use it all, keep that 3 kW steal for the same 15 min hold (`number.kotiakku_goe_direct_hold_minutes`) while leftover stays at least 6 kW, then drop it. Group `lot` starts from the **total** leftover and is raised if needed so both per-charger `amp` caps fit (still at most group lot 50). Each charger’s `psm` / `amp` comes from **its** allocation. HA reads per-charger `nrg` (MQTT or `sensor.go_echarger_<serial>_nrg`) and never writes `lop`. Slot defaults are charger 1 → 1, charger 2 → 2, and so on (charger 1 highest). Set equal numbers to share leftover the way load balancing does.

When one charger is full-power (`lot` 50 / `amp` 32), surplus does **not** write a smaller leftover `lot` to another SolarPriority or SolarAndGrid charger — last writer would shrink the group to leftover amps and both cars would be stuck at that cap, so app priorities could not give 32 A to the cheap-hour session. Surplus keeps group `lot` at 50 and sets leftover `amp` / `psm` / `frc` on the surplus charger only. That leftover `amp` is the surplus energy cap, not an HA ranking. Combined demand may exceed 50 A (`32` + leftover `amp`); **app priorities then split the 50 A group.** HA does not reserve 32 A for the cheap-hour charger by capping surplus `amp`.

`amp` cannot be 0 (official range 6–32). Stopping surplus-style or full-power charging is **`frc=1`** (force off), not `amp=0` and not **`frc=0`** (Neutral). In Basic/default charging mode Neutral keeps charging (`ChargingBecauseFallbackDefault`). Cheap-hour and surplus start still use **`frc=2`**. `amp`/`psm` alone do not start a session.

Budget from leftover watts (floored amps, Finnish 230 V):

1. If leftover ≥ 3-phase leftover (default 4140 W = 6 A × 230 V × 3) → `psm=2`, `lot = leftover // (230 × 3)`
2. Else → `psm=1`, `lot = leftover // 230`
3. `lot` is at least 6 A and at most group lot (default **50 A**). Per-charger `amp = min(max amp, lot)` (default **32**). 3-phase amp is leftover ÷ (230 × 3), not stuck at 6 A. If a car is already at the published amp cap and leftover would allow more, it is offered leftover again so amp can rise.
4. A published `psm` change (1-phase ↔ 3-phase) waits the same hold minutes (default 15) **both up and down**. CCS cannot switch phases in-session: go-e pauses charging for several seconds, and Tesla can send a charging-stopped / interrupted app alert (often CP_a055). Holding `psm` does **not** freeze amp: leftover is still budgeted on the phase that is actually running. 1→3: 1-phase leftover, capped at max amp (8 kW → 32 A, not the pending 11 A 3-phase). 3→1: 3-phase min amp (6 A), not the pending 1-phase amp. The first surplus start has no last `psm`, so it picks 1- or 3-phase immediately. Full-power force-on writes `psm=2` immediately.

Start still needs SoC ≥ surplus SoC on (default **92%**) and leftover ≥ start leftover (default **2000 W**). After that, do **not** cut to zero every time leftover dips. While the session is on:

- Leftover ≥ low hold leftover (default **1000 W**) and SoC ≥ SoC on minus hysteresis (default **90%**) → budget tracks leftover
- Leftover below 1000 W, **or** SoC below 90%, **or** Kotiakku SoC / solar / house unknown or unusable → keep **6 A** for up to the low hold minutes (default 15). If the last surplus `psm` was 3-phase, stay 3-phase 6 A for that window so Tesla is not interrupted twice (phase switch, then stop). Chatter around 1000 W does not reset that timer: leftover must reach the start leftover (2000 W) to cancel the hold
- Leftover wants the other phase → keep the current `psm` for those same 15 min, then switch. Leftover returning to the current phase cancels the timer
- Second surplus charger already on, leftover then shrinks so the high-priority car would use it all → keep the 3 kW second-car floor for those same 15 min, then drop it. That steal needs two plugged cars that are actually taking leftover; an unplugged or finished first charger does not keep 3 kW on the next one. Steal is not applied when leftover itself is below **6 kW** (3 kW per car)
- That low hold for the whole duration → `frc=1` (force off)
- Recovered leftover (at least the start leftover, 2000 W), SoC, or sensors cancel the hold timer. A warning is logged when Kotiakku values are unusable. Surplus cannot **start** while those sensors are unusable. Restart loses remaining hold minutes (same as the other holds).

MQTT order: start is `fup` / `psm` / `lot` / `amp` then `frc=2`; stop is `frc=1` then `fup` false.

## 3. Deploy in Home Assistant

This GitHub repo is the source. Home Assistant loads the integration from **`<config>/custom_components/kotiakku_goe_direct/`**. `<config>` is the directory that also holds `configuration.yaml` (HAOS: `/config`, or the Samba `config` share).

Do **not** copy the whole repo into `custom_components/`. Only the inner `kotiakku_goe_direct` folder belongs there. Do not also run the old YAML surplus or charge automations.

### Prerequisites

- MQTT in Home Assistant can **publish** to `go-eCharger/<serial>/<key>/set`. This integration depends on the MQTT integration.
- Chargers: MQTT writes allowed (`mcr=false`), load balancing on (`loe=true`), group total 50 A, charger max 32 A. Set leftover priority on the HA device (`number.kotiakku_goe_direct_priority_<serial>`). App `lop` still applies to the 50 A group.
- Sensors you will pick already exist: Nordpool (with `raw_today` / `raw_tomorrow`), go-e Controller Car-power 5-min mean, Kotiakku SoC / solar / house, charger entities. Optional: solar today (full-day kWh) and tomorrow kWh.
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

Policy pickers start at **Force off**. That charger does not charge until you pick SolarPriority, SolarAndGrid, or Force on (or turn on Force On Until Unplug).

On load — and again when Home Assistant has finished starting — the integration asks every wired sensor (Kotiakku, Forecast.Solar, Nordpool, charger car/power) to update before it plans, so restored leftover values are not used.

After it exists, **Configure** edits charger entities/serials/priorities and Controller / Kotiakku wiring. Surplus numbers, group lot, window bounds, the price text, leftover priorities, policies, and Force On Until Unplug switches are entities on the **Kotiakku go-e Direct** device so they can go on a dashboard.

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
  solar_today_entity: sensor.energy_production_today
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

Optional 48 h leftover / price graph: [§7](#7-graphs). HACS does not copy that YAML file.

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

- **SolarPriority** — one cheap window on the hours that have both spot prices and solar forecast (prices-only if forecast is missing). High-solar hours (≥ `number.kotiakku_goe_direct_offsun_hour_kwh`, default **1 kWh**, elevation-weighted from local midnight) are dropped. The search takes the cheapest `window min` seed, then grows toward the cheaper neighbor while the average stays under flex (20% **or** 0.02 €/kWh, whichever is looser) and at most `window max`. Clock ticks do not move that window: a cheapest stretch that has already ended stays the plan (visible in the past) and is not used for 22 kW. No 22 kW when **today's** full-day solar is at least `number.kotiakku_goe_direct_solar_enough_kwh` (default **40 kWh**) until local sunset; after sunset the same knob gates on **tomorrow**. Cloudy tomorrow allows night 22 kW. Surplus can still write that charger. A previous Cheapest / Supercheap / Longest / Earliest select is restored as SolarPriority.
- **SolarAndGrid** — same cheap window as SolarPriority (same off-sun hours dropped from the search). Still 22 kW in that window when enough-solar would skip SolarPriority. Surplus leftover outside the window.
- **Force on** — that charger full power now
- **Force off** — never charges (`frc=1`)

`switch.kotiakku_goe_direct_until_unplug_<serial>` (**Force On Until Unplug**) is a one-shot override, not a policy. Turn it on to force that charger to 22 kW regardless of the select. It stays on through charging and a full battery (go-e Complete). It turns itself off when **that** car **unplugs**. The policy select is unchanged, so SolarPriority / SolarAndGrid / Force off / … continues afterwards. Turn the switch off to cancel. Surplus skips that serial while the switch is on. A previous install that had **Force on until unplug** selected is migrated onto this switch and the stored previous policy.

Leftover surplus writes **SolarPriority** and **SolarAndGrid** chargers. Surplus skips **Force off**, and also skips a charger that is already full-power (**Force on**, a cheap window, or Force On Until Unplug). Spot-price windows and force-on do **not** read Kotiakku SoC / solar / house. Gridle going unknown only affects leftover surplus. HA still does not write app charger priorities (`lop`).

Windows are planned from `raw_today` / `raw_tomorrow` (or `today` / `tomorrow`), clipped to days that also have solar kWh when any forecast exists. The search finds the cheapest contiguous min-hours seed (ceiling is ignored while scoring), aborts if that seed’s average is above the ceiling (default **0.2**), then grows by one native slot at a time. Flex is the looser of percent-of-|seed| and a fixed €/kWh. Max hours is a cap, not a target. Off-sun hours still split islands; only one window is filled. The window is a function of prices, solar clip, the off-sun mask, and knobs — not of the clock — so 15-minute ticks do not slide it. Tomorrow’s curve typically appears sometime after 14:00 local and is a new environment (the plan may jump). Helsinki midnight may change the window when Nordpool `raw_today` and the calendar day roll. The plan also replans when today’s or tomorrow’s kWh, flex, or the Off-sun hour knob change.

Full-power MQTT on that charger: `fup` false, `psm=2`, `amp=32`, `lot=50`, `frc=2`. After: `frc=1` then `fup` false.

| Entity | Default | Role |
| --- | --- | --- |
| `select.kotiakku_goe_direct_policy_<serial>` | Force off | SolarPriority / SolarAndGrid / Force on / Force off. Force off never charges |
| `switch.kotiakku_goe_direct_until_unplug_<serial>` | off | Force On Until Unplug: 22 kW until that car unplugs. Stays on at a full battery (Complete). Does not change the policy select |
| `number.kotiakku_goe_direct_window_min_h` / `kotiakku_goe_direct_window_max_h` | 2–5 h | Seed length and grow cap. Equal min/max is a fixed-length window. Min 0.25 h is one 15-minute slot |
| `number.kotiakku_goe_direct_window_flex_pct` / `kotiakku_goe_direct_window_flex_eur` | 20 / 0.02 | Grow may raise the window average by the looser of these above the seed. Both 0: no grow |
| `sensor.kotiakku_goe_direct_window` | planned start | Planned window, including one that already ended. State is the start timestamp; `end`, avg, and `window_N_*` are attributes. `binary_sensor.kotiakku_goe_direct_window_active` is on while now is inside a window |
| `number.kotiakku_goe_direct_electricity_price_ceiling` | 0.2 | Safety: no window if the cheapest seed average is above this. Grow will not add a slot above it |
| `text.kotiakku_goe_direct_electricity_price_sensor` | from setup | Electricity price sensor id |
| `number.kotiakku_goe_direct_soc_on_pct` / `kotiakku_goe_direct_soc_hyst_pct` | 92 / 2 | Surplus SoC start (92%) and low-hold below 90% |
| `number.kotiakku_goe_direct_surplus_start_w` | 2000 W | Leftover to start surplus |
| `number.kotiakku_goe_direct_priority_<serial>` | slot 1–4 → 1–4 | Leftover offer order. 1 is highest, 99 is lowest. YAML/config seeds first add; then this entity. Equal values share leftover |
| `number.kotiakku_goe_direct_next_surplus_min_w` | 3000 W | Unequal HA leftover priority: per-car surplus floor. If unused leftover after the higher-priority car is below this (and above the remainder floor), steal this much from the first — only if the first would still keep this much (3+3 kW minimum) |
| `number.kotiakku_goe_direct_remainder_floor_w` | 500 W | Unequal HA leftover priority: remainder at or below this does not *start* the next car. Already-on next car keeps 3 kW for the hold minutes if leftover is still at least 6 kW |
| `number.kotiakku_goe_direct_low_hold_w` | 1000 W | Leftover below this is the 6 A low hold |
| `number.kotiakku_goe_direct_settle_s` | 5 s | Wait after Gridle updates |
| `number.kotiakku_goe_direct_hold_minutes` | 15 min | Hold duration (leftover, SoC, second-car leftover gone, or 1↔3 `psm`) |
| `number.kotiakku_goe_direct_voltage_v` / `kotiakku_goe_direct_min_a` / `kotiakku_goe_direct_max_a` | 230 / 6 / 32 | Budget math |
| `number.kotiakku_goe_direct_phase3_min_w` | 4140 W | Switch leftover to 3-phase (after the 15 min `psm` hold) |
| `number.kotiakku_goe_direct_group_lot_a` | 50 A | Load-balancing group current cap. YAML `eco_lot` still seeds this |
| `number.kotiakku_goe_direct_solar_enough_kwh` | 40 kWh | SolarPriority: no 22 kW when today's full-day kWh ≥ this until sunset, then when tomorrow ≥ this. SolarAndGrid ignores this skip. Missing tomorrow after sunset is not enough (night 22 kW allowed). 0 disables. `binary_sensor.kotiakku_goe_direct_solar_enough` is that condition |
| `sensor.kotiakku_goe_direct_solar_today_kwh` | from Configure | Today's full-day kWh. Attribute `source` is the picker entity |
| `sensor.kotiakku_goe_direct_solar_tomorrow_kwh` | from Configure | Tomorrow kWh. Attribute `source` is the picker entity |
| `sensor.kotiakku_goe_direct_solar_gating_kwh` | today or tomorrow | kWh that currently gates the 22 kW skip (today until sunset, then tomorrow) |
| `sensor.kotiakku_goe_direct_solar_gating_day` | `today` / `tomorrow` | Which day's kWh is gating |
| `sensor.kotiakku_goe_direct_solar_kwh` | max(today, tomorrow) | Headline forecast. Attributes: `source_today`, `source_tomorrow`, `enough_solar`, `offsun_hour_kwh` |
| `number.kotiakku_goe_direct_offsun_hour_kwh` | 1 kWh | Drop a local hour from the search when its expected forecast energy ≥ this. 0 disables. Dawn/dusk/night under 1 kWh stay searchable |

YAML `soc_on`, `group_lot` (legacy `eco_lot`), charger `priority`, … only seed those entities on first add. After that, change the device entities. If `number.kotiakku_goe_direct_next_surplus_min_w` still shows 11000 W from an older restore, set it to 3000 W.

## 6. Smoke checks

After the first surplus write, `go-eCharger/<serial>/lot/result`, `amp/result`, `psm/result`, `frc/result`, `fup/result` should be `true`.

| Expect | What you should see |
| --- | --- |
| SoC ≥ 92% and leftover ≥ 2000 W, both policies Force off, **equal** HA leftover priority | No leftover MQTT. Both chargers stay `frc` 1 |
| Only charger 1 configured, SolarPriority | Leftover MQTT on that charger only. No second-car steal |
| SoC ≥ 92% and leftover ≥ 2000 W, SolarPriority, car Idle / unplugged | Leftover MQTT `frc=2`. Allowd to charge / Force State On before WaitCar |
| SoC ≥ 92% and leftover 8 kW, **SolarPriority**, **unequal** HA leftover priority, high at 6 A 3-phase | High is offered leftover (11 A), not locked at 6 A. Second car waits until high leaves unused leftover |
| SoC ≥ 92% and leftover 12 kW, high taking 10 kW | **9 kW + 3 kW**. Remainder 2 kW is above 500 W, so steal up to the 3 kW floor |
| Leftover 6 kW, high taking it all, second car already on | **3 kW + 3 kW**. Minimum split; grace still holds |
| Leftover 4.5 kW, high taking it all, second car already on | No steal (would be 1.5+3). High keeps 4.5 kW; drop the second car |
| Higher-priority car taking 7.5 kW of 8 kW leftover | Only the higher-priority charger: 500 W remainder is the dead zone (do not start the second car) |
| Second car already surplus-charging, leftover then drops so high would use it all | Keep **3 kW** on the second car for 15 min, then drop it |
| Higher-priority car Complete / WaitCar, leftover 8 kW | Lower-priority charger gets leftover if it still meets 6 A. WaitCar still gets an offer so it can start; no steal |
| Higher-priority car taking 10 kW of 18 kW leftover | Lower-priority charger gets the remaining 8 kW (already ≥ 3 kW, no steal) |
| Leftover 4 kW, unequal HA leftover priority, both wanting surplus | Only the higher-priority charger: it wants all 4 kW |
| Surplus on, leftover collapses below 1000 W | `lot` 6 for up to 15 min (stay 3-phase 6 A if that was the last `psm`), then `frc` 1 |
| Surplus on 1-phase, leftover rises to 8 kW | Stay `psm` 1, `amp` 32 for 15 min, then `psm` 2 / 11 A. Amp still tracks leftover while held |
| Surplus on 3-phase, leftover drops to 3 kW | Stay `psm` 2, `amp` 6 for 15 min, then `psm` 1 / 13 A |
| SoC 90–91% during a session | Keep tracking leftover. Not a hold, not a stop |
| SoC &lt; 90% | Same 6 A low hold as leftover &lt; 1000 W. Not an immediate cut; `frc=1` only after the hold expires |
| Kotiakku SoC / solar / house unknown or unusable | Warning in the log; same 6 A low hold on **SolarPriority** / **SolarAndGrid** surplus chargers. Stop surplus only if still unusable after 15 min |
| SolarPriority + window binary on, gating day's solar under 40 kWh | **That** charger `psm` 2, `amp` 32, `lot` 50, `frc` 2 even if Kotiakku is unknown. Surplus skips **that** serial only; it does not lower group `lot`. Leftover `amp` on another **SolarPriority** / **SolarAndGrid** charger is not cut to leave 32 A; app `lop` splits the 50 A group |
| SolarPriority, window on, today's kWh ≥ 40 kWh before sunset | No full-power (wait for today's PV). Surplus may still write that charger. `binary_sensor.kotiakku_goe_direct_solar_enough` on |
| SolarAndGrid, window on, today's kWh ≥ 40 kWh before sunset | **That** charger still 22 kW. Surplus skips **that** serial |
| SolarAndGrid, window off, leftover ≥ 2000 W | Leftover MQTT (`frc=2`) |
| SolarPriority, after sunset, tomorrow ≥ 40 kWh | No night 22 kW. Surplus may still write that charger |
| SolarPriority, after sunset, today 80 kWh and tomorrow 10 kWh | Night cheap hours **are** 22 kW (day2 is not enough) |
| SolarPriority, hour with ≥ 1 kWh expected solar | Dropped from the window search; no SolarPriority 22 kW in that hour |
| SolarPriority, today 8 kWh and tomorrow 6 kWh | Night cheap hours still 22 kW (hours under 1 kWh stay searchable) |
| SolarPriority, forecast unknown or unset | Search all available spot slots (nothing excluded, not enough solar) |
| Force off during a price window | That charger stays off (`frc` 1). Surplus does not write that charger |
| Force On Until Unplug switch on | That charger 22 kW until **its** car unplugs. Full battery (Complete) keeps it on. Policy select stays put |

## 7. Graphs

The 48 h Finnish year-round cases (spot, SolarPriority window, leftover, per-charger phase / amp / commanded kW) can be drawn (Off-sun hour 1 kWh, skip 22 kW when the gating day's kWh ≥ 40):

```bash
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot
```

Live Home Assistant: one dashboard tab, no helper sensors. Install [apexcharts-card](https://github.com/RomRider/apexcharts-card) from HACS, then copy [`homeassistant/dashboards/kotiakku_goe_direct_48h.yaml`](homeassistant/dashboards/kotiakku_goe_direct_48h.yaml) as a YAML dashboard, or paste the `views:` list into a UI dashboard's raw editor.

The spot chart is `raw_today` / `raw_tomorrow` (including the 14:00 day-ahead curve) with the SolarPriority window from `sensor.kotiakku_goe_direct_window` attributes (`windows` / `window_N_start` / `window_N_end`) and off-sun hours from that sensor's `blocked` list. Spot itself is read through `text.kotiakku_goe_direct_electricity_price_sensor`. Planned 22 kW uses each charger's policy + that window, only for slots that have not ended: SolarPriority skips before today's sunset when **today** ≥ enough solar, and after sunset when **tomorrow** ≥ enough; SolarAndGrid still draws 22 kW in the window. A finished window stays on the spot chart and is not drawn as 22 kW. Leftover past is `|solar| − |house| + |ev|` from recorder 5-minute statistics of the three power sensors you already have — edit those entity ids (and kW vs W) in the leftover `data_generator`. Replace fake serials `111111` / `222222`. One charger: delete the charger 2 series. Display-only; it does not write MQTT.

## Tests

```bash
python3 custom_components/kotiakku_goe_direct/tests/test_serial.py
python3 custom_components/kotiakku_goe_direct/tests/test_planner.py
python3 custom_components/kotiakku_goe_direct/tests/test_clock_roll.py
python3 custom_components/kotiakku_goe_direct/tests/test_spec_windows.py
python3 custom_components/kotiakku_goe_direct/tests/test_spec_surplus.py
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py
python3 custom_components/kotiakku_goe_direct/tests/test_finland_year.py --plot
```
