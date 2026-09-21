# Behavior spec

Extracted from the implementation on `main` (integration 0.6.0). This is the behavior a reimplementation has to preserve until you edit it. Rules marked **product** are what the running system does. Rules marked **accident** are artifacts of this codebase. Delete or rewrite the accidents while massaging this document. Do not treat the README as a second source: where they differ, this file follows the code.

The controller should not grow a strategy class per policy. Section 2 is the shape to reimplement.

## 1. Outside the box

One hub, one to four go-e Gemini chargers, one shared load-balancing group.

Writes, in order:

- Start: `fup=false`, `psm`, `lot`, `amp`, then `frc=2`.
- Stop: `frc=1`, then `fup=false`.

Topics are `go-eCharger/<serial>/<key>/set`. QoS 0, no retain.

Never publish `frc=0`. In Basic/default mode Neutral keeps charging. Never publish to `go-eController/…`. Never write `ama`, `loe`, `loty`, or `lop`. App `lop` still splits the group current inside go-e. HA leftover priority is a different number.

Chargers are assumed to be in Basic/default, load balancing on, group total 50 A, per-charger max 32 A. Those last two are knobs (`group_lot` default 50, `max_amp` default 32).

## 2. Pipeline, not strategies

Each apply reads the world once, then runs these stages in order. Stages are pure functions of an explicit session. Only the last stage talks to MQTT.

```text
snapshot
→ advance until-unplug
→ roles
→ leftover session decision
→ allocate the surplus set
→ advance keep          (sees who is taking)
→ if keep changed: roles + decision + allocate again
→ one command per charger
→ diff against live frc/amp/lot/psm
→ publish
```

Four policies and four roles look like strategies. They are not.

Role is a total order for one charger, evaluated for every charger before anyone publishes:

1. Full power (22 kW) wins.
2. Else after-charge-complete keep.
3. Else leftover surplus, only for SolarPriority and SolarAndGrid.
4. Else off.

A cheap hour ending must not `frc=1` a charger that leftover is about to write. A surplus charger must not publish a smaller group `lot` while another charger is at 22 kW or keep, because the last writer would cap the whole group. Keep's next state depends on whether some other surplus charger is taking power. Those three rules cross every policy. A `FullPowerStrategy` / `SurplusStrategy` / `KeepStrategy` that each publish would recreate the bugs the pipeline is there to prevent.

What varies per policy is five booleans, not an algorithm:

| Policy | Full power when | Surplus when not full and not keep |
| --- | --- | --- |
| Force on | always | no |
| Force off | never | no |
| SolarAndGrid | now is inside a planned window | yes |
| SolarPriority | inside a window, and enough-solar is false | yes |
| Until-unplug switch on | always, and the policy select is ignored for that decision | no |

Legacy select values `Cheapest`, `Supercheap`, `Longest`, and `Earliest` read back as SolarPriority. `Force on until unplug` is no longer a policy. It is the until-unplug switch. **Accident:** the mapping exists twice (`const.py` and `planner.py`).

The one replaceable stage, if you ever need one, is "allocate the surplus set". The product has a single allocator. Do not split it per charger.

## 3. Knobs

YAML and config-entry values seed entities on first add. After that, the entities win. Priorities are the exception that also swap (below).

| Knob | Default | Range |
| --- | --- | --- |
| Window min / max hours | 2 h / 5 h | 0.25–24 h, step 0.25. If min > max, they swap |
| Price ceiling | 0.2 | used as a price, not a score |
| Flex percent / flex €/kWh | 20 / 0.02 | negative becomes 0. Headroom is the larger of `abs(seed) * pct/100` and the euro amount. Both 0: no grow |
| Enough solar | 40 kWh | ≤ 0 disables the 22 kW skip |
| Off-sun hour | 1 kWh | ≤ 0 disables dropping hours from the price search |
| SoC on / hysteresis | 92% / 2% | low hold when SoC < on − hyst |
| Surplus start leftover | 2000 W | |
| Low-hold leftover | 1000 W | |
| Hold minutes | 15 min | one duration for low hold, phase hold, and steal hold |
| Next-surplus minimum | 3000 W | per-car floor when stealing |
| Remainder floor | 500 W | at or below this, do not start the next car |
| Voltage / min amp / max amp | 230 V / 6 A / 32 A | |
| Max 1-phase amp | 32 A | surplus 1-phase cap, clamped to min–max amp |
| Group lot | 50 A | 6–64 |
| Keep amp / keep phase | 6 A / 3-phase | phase select: 1-phase → `psm=1`, 3-phase → `psm=2` |
| Preferred start phase | 1-phase | first surplus start only, when both phases can offer leftover |
| Leftover priority | slot order 1, 2, 3, 4 | 1 is highest, 99 is lowest. Must be unique |
| Kotiakku solar/house in kW | true | magnitudes, then ×1000 |
| Controller mean in kW | false | |
| Offer wait | 15 s | constant, not a knob |
| Keep probe | 60 s | constant |
| Take threshold | 100 W | "has started". Not the keep-probe band |
| Idle-complete band | 400 W | Complete below this is idle |
| MQTT batch | 2 s | constant. `number.…_settle_s` (default 5) is unread. **Accident:** delete that entity |
| Safety replan | 15 min | forces a republish of an unconfirmed echo |

Leftover priorities are unique. Setting a charger's number to a value another charger holds swaps the two. Setup, Configure, and the priority number all do this.

## 4. Inputs

Price sensor attributes, first present key wins:

- Today: `raw_today`, `raw_today_prices`, `today`.
- Tomorrow: `raw_tomorrow`, `raw_tomorrow_prices`, `tomorrow`.
- `tomorrow_valid` true / `on` / `true` / `True` means tomorrow's curve is present even if no tomorrow slot was parsed.

A series is either dict slots (`start`/`from`/`begin`, `end`/`to`/`until`, `value`/`price`/`price_ct`) or a list of prices spread across the local day: 96 → 15 min, 48 → 30 min, 24 → 1 h, otherwise `86400 / n` seconds. Missing prices are holes. Slots are sorted by start.

Kotiakku SoC, solar, and house are unusable when missing, blank, `unknown`, `unavailable`, `none`, `nan`, or not a number. Any one unusable blocks a new surplus start and counts as the low-hold condition. It does not block 22 kW.

Forecast today / tomorrow are energy. Power units (`W`, `kW`, …) are rejected. `Wh` → ÷1000, `MWh` → ×1000. Otherwise the number is kWh.

Charger power for one serial: live MQTT `nrg` if a sample has arrived, else `sensor.go_echarger_<serial>_nrg` or the resolved power entity. `nrg` arrays use index 11 (go-e v2 total watts), absolute value. A numeric sensor uses its state; unit `kW` multiplies by 1000. Unknown stays unknown (not 0), except where a rule below says otherwise.

Car state is lowercased with spaces and underscores removed.

- Plugged: `2`, `3`, `4`, `5`, `charging`, `waitcar`, `complete`, `error`, `waitforcar`, `waitforvehicle`, `finished`, `connected`.
- Charging: `2`, `charging`.
- Complete: `4`, `complete`, `finished`.

Site latitude and longitude come from Home Assistant. Missing site uses 60.17 N, 24.94 E. Solar elevation is a no-refraction approximation: declination `23.45° sin(360°/365° (day-of-year − 81))`, hour angle from UTC and longitude. Weight is `sin(elevation)` while elevation > 0, else 0. Samples are every 15 minutes.

## 5. Price windows

**Product.** The plan is a function of prices, the off-sun mask, the forecast clip, and the knobs. The clock does not move a window. A valley that already ended stays the plan and is visible in the past. It does not turn on 22 kW. `choose()` ignores `now` and the previous plan. Replan when prices, forecast kWh, flex, ceiling, hours, or the off-sun knob change, on the 15-minute safety tick, and when tomorrow's curve appears (that is a new environment, and the plan may jump).

Steps:

1. Collect today and tomorrow slots.
2. If both forecast kWh values are missing, keep every price slot. If either is present, drop a day that does not have that day's kWh. Today's slots are `today_start ≤ start < tomorrow_start`. Tomorrow's are `tomorrow_start ≤ start < day_after`.
3. Spread today's kWh over local midnight–midnight and tomorrow's kWh over the next local day, weighted by the solar samples above. Drop every slot that overlaps an hour whose expected energy is ≥ the off-sun knob. Merge adjacent blocked hours. A non-positive knob drops nothing. Unknown energy drops nothing for that day. A gap of more than 60 seconds between slot end and the next slot start splits an island.
4. Seed: among contiguous runs on one island, take the shortest prefix that lasts at least min hours (the search stops at the first length that qualifies). The winner is the lowest duration-weighted average. Ties go to the earlier start. The ceiling is not used while scoring the seed.
5. If that seed's average is above the ceiling, there is no window.
6. Grow one neighboring slot at a time toward the cheaper neighbor. A neighbor is legal when it is on the same island (gap ≤ 60 s), its own price is ≤ ceiling, the new duration is ≤ max hours, and the new duration-weighted average is ≤ seed average + flex headroom. If both sides qualify, the cheaper slot price wins. A price tie prefers the left side. Flex headroom is the larger of the percent-of-absolute-seed and the euro amount. No flex: the seed is the window.
7. Second window. Let the overnight span be local today 22:00 through the end of tomorrow (22:00 is wall time, not start-of-day plus 22 hours). If tomorrow still has a searchable slot and the first window does not overlap that span, plan another window whose min-hours seed lies entirely inside tomorrow. The same grow may walk into today. If that seed is above the ceiling, or it cannot be found, there is no second window.

22 kW is on when `start ≤ now < end` for any planned window. Abutting windows (`end` of one equals `start` of the next) do not turn 22 kW off at the shared boundary, because the end is exclusive and the next start is inclusive. The two windows stay separate on the sensor. They are not merged into one span. A real gap between them does turn 22 kW off.

The window sensor's state is the first window's start, or unavailable when there is no window. Attributes carry end, average, the list, per-window start/end/average (up to 16), reason, tomorrow-ok, slot count, and the blocked ranges. `binary_sensor.…_window_active` is the 22 kW clock test above, independent of policy.

A boundary timer fires at the next window start or end and runs apply. It does not replan by itself.

## 6. Enough solar

**Product.** This only suppresses 22 kW for SolarPriority. SolarAndGrid, Force on, and until-unplug ignore it. It does not read Kotiakku SoC, solar, or house.

Gating day is today until both of these are true: tomorrow's spot curve is present, and today's last usable solar hour has ended. Usable means expected hour energy ≥ the off-sun knob. A non-positive off-sun knob treats any hour with expected energy > 0 as usable. No qualifying hour, unknown today-kWh, or polar night means "usable solar is already gone", but the gate still stays on today until tomorrow's prices exist. After the flip, gating kWh is tomorrow's full-day value. Missing tomorrow after the flip is not enough, so night 22 kW is allowed.

Enough-solar is true when gating kWh ≥ the threshold. Unknown energy or a non-positive threshold is not enough.

Headline forecast (`sensor.…_solar_kwh`) is `max(today, tomorrow)`, ignoring missing values. That headline is not the gating value.

## 7. Leftover watts

**Product.**

```text
solar_w = abs(solar) in watts
house_w = abs(house) in watts
ev_w    = effective EV watts
```

Do not abs the result. Negative is a deficit.

Effective EV:

- Prefer per-charger `nrg`. Sum known charger watts. Unknown for every charger counts as no `nrg`.
- Controller entity is the Car-power 5-minute mean. Unknown controller is not usable.
- Both known and `nrg` sum > 0: use `min(controller, nrg sum)`.
- Only `nrg`: use `nrg`.
- Only controller: use controller.
- Neither: 0 W.

Add EV back only when the house figure already contains it. If `ev_w > 0` and `house_w < ev_w − max(1000, ev_w / 5)`, house does not include the car (CT misses the charger, or the 5-minute mean still shows a car that left). Leftover is then `solar − house` with no EV add-back. Otherwise `solar − house + ev`.

`leftover_w` on the surplus sensor is that value, before keep. `sensor.…_available_surplus` subtracts every keep charger's `nrg` (unknown keep `nrg` subtracts 0, and every known watt counts, including Complete trickle under 400 W). A negative result stays negative.

## 8. Surplus session

State that survives a restart: `session` (leftover is on), `split_session` (two or more cars were taking).

State that does not survive a restart: low-hold timer, steal-hold timer, phase-hold timers, offer-wait timers, keep-probe timer. Remaining minutes are lost. The 15-minute safety tick is the backstop.

`window_ok` means SoC, solar, and house are all usable. It is not the price window.

Let `soc_low` mean window_ok and SoC < soc_on − hyst. Let `leftover_low` mean leftover < hold_min_w. While the low-hold timer is already armed, leftover is "low" until it reaches `max(hold_min_w, start_min_w)` (the controller passes start leftover as the exit). Chatter around 1000 W does not cancel the timer. Start still requires leftover ≥ start leftover.

`in_low_hold` = not window_ok, or leftover_low, or soc_low.

- `write_off` = session is on, the low-hold timer has fired, and still in_low_hold.
- `write_on` = not write_off, and (session is on, or (window_ok and SoC ≥ soc_on and leftover ≥ start)).
- `arm_floor` = (write_on, or session and not write_off) and in_low_hold.
- `use_floor_budget` = write_on and in_low_hold. Setpoints drop to the 6 A floor. Phase stickiness is section 11.

Cannot start while sensors are unusable. An already-open session holds 6 A for hold minutes, then stops if still unusable. Recovered sensors cancel the timer. Arming the timer when it is already armed does not restart it. Cancelling it logs and clears it.

`write_on` with at least one surplus-role charger opens the session. Otherwise, if the session was open, or a leftover setpoint was remembered, or write_off: close the session, clear split state, and cancel offer waits.

## 9. Who is taking

Cap for one charger is `min(leftover, max_amp × volts × 3)`. The cap is always the 3-phase maximum, including when the car is on 1-phase. **Accident** if you intended the 1-phase cap here. The code does not apply it.

`charger_take_w`:

- Not plugged → 0.
- Complete and `nrg` unknown or < 400 W → 0 (idle Complete).
- Complete and `nrg` ≥ 400 W → `min(nrg, cap)`.
- Plugged but not charging (Idle, WaitCar, unknown, error) → 0.
- Charging and (`nrg` unknown or < 100 W) → the cap. A Tesla that has been told to charge but is not yet drawing still counts as taking the offer.
- Otherwise → `min(max(nrg, 0), cap)`.

`surplus_want_w` then adjusts that take. Below 100 W, or unknown, the raw take is kept (unknown becomes "wants all leftover"). At or above 100 W, if the car is within one phase-watt of the last published amp cap and leftover would now budget a higher amp, or would move 1-phase → 3-phase, treat the car as wanting all leftover. That is what lets amp rise. MQTT amp tracks leftover, not the last measured take.

Offer-pending, as the controller computes it: surplus role, wanted watts < 100, and the 15-second timer for that serial has not yet expired. A car that has never been offered is pending. Pending is a property of "not taking yet", not of "we published N seconds ago", until the timer fires and moves the serial into the expired set. Taking clears the expired flag.

## 10. Allocation

Input is the ordered surplus-role serials, HA priorities, car states, wanted watts, leftover watts after keep, and the pending / keep-cut sets. The `plugged` map is accepted and ignored.

Idle Complete (section 9) is not eligible, not backfilled, and not a steal participant, unless that serial is keep-cut (section 13). Keep-cut Complete is eligible so `frc=2` can pull a Tesla back out of Complete.

Leftover ≤ 0 or nobody eligible → no allocations.

If any eligible charger has no priority, or every eligible charger has the same priority, or there is one eligible charger: every eligible charger is allocated the full leftover. go-e splits equal cars. HA does not split the watts.

Otherwise sort by priority ascending (1 first), then by configuration order.

Walk from the best priority:

- While the front car is not taking and not pending, set it aside as leading. It stays armed at full leftover, and it is not a group-lot share.
- While the front car is not taking and pending, set it aside as leading and as an overdraw serial.

If that empties the list, fall back to "everyone gets full leftover".

If one car remains, it gets full leftover. Leading cars are also armed at full leftover. If any overdraw serial exists and any allocated car is taking, those overdraw serials are lot-shares too (temporary over-draw).

If several cars remain, walk them with a shrinking remainder. Call the first the high car.

- High car: if remainder cannot support 6 A at the phase leftover would pick, stop. Otherwise allocate `min(remainder, charger cap)` when its take is 0 or any overdraw serial exists, else allocate its take. Subtract its take from the remainder. If any overdraw serial exists, stop the walk. Do not start a further car during the offer wait.
- Next car, remainder ≥ next-surplus minimum and remainder can support 6 A: allocate `min(remainder, cap)`, subtract its take. If that car is itself pending, stop. Do not start a car behind a pending offer.
- Next car, remainder ≤ remainder floor: do not start it, unless a steal-hold is already running and has not expired (below).
- Steal: leftover itself ≥ `2 × next-surplus minimum`, the previous car's take ≥ 100 W, no overdraw serials, this car is not pending, and both the reduced high share and the next-surplus minimum still support 6 A. Then high's allocation becomes `previous_take + remainder − next_surplus_min`, and this car gets `next_surplus_min`. Example at the defaults: 12 kW leftover, high taking 10 kW, remainder 2 kW → high keeps 9 kW, next gets 3 kW. 4.5 kW leftover does not steal (would leave the high car under 3 kW).
- Else if remainder still supports 6 A, allocate it even below the next-surplus minimum (the next car is already in this branch only when steal failed).
- Else stop.

After a taking high car, if the remainder at that moment is ≤ the remainder floor, a steal-hold was already on, it has not expired, and at least two allocated cars are taking: keep the hold armed. The hold lasts `hold_minutes` and does not restart while armed. When it fires, the next apply is marked split-expired and the steal is allowed to drop.

Whenever any eligible charger with a worse priority number has an allocation, every better eligible priority is put back into the allocation map at full leftover (armed, `frc=2`). Those backfills are lot-shares only while overdraw is on. The controller repeats this for a serial the allocator left out: if a worse priority is allocated and this serial is not idle Complete (keep-cut excepted), it still gets a leftover setpoint at the full post-keep leftover (or the 6 A floor while in low hold).

Group `lot` during overdraw sums amps even when they are equal, so the pending arm and the taking car can both draw. Otherwise equal amps do not sum (two 17 A cars must not turn 12 kW into 34 A). Differing amps sum, still capped at group lot.

## 11. Phase and amps

**Product.** Finnish 230 V unless the voltage knob says otherwise. `psm=1` is 1-phase, `psm=2` is 3-phase. Amp is never 0. The legal floor is min amp (default 6).

Watts a phase can offer = `amp × volts × phases`, with amp = clamp(`available // (volts × phases)`, min amp, cap). 1-phase cap is max-1-phase-amp. 3-phase cap is max amp. 3-phase is available only when leftover ≥ min amp × volts × 3 (default 4140 W).

Wanted phase:

- Active 3-phase stays 3-phase while 3-phase is available. Below the 6 A 3-phase floor it wants 1-phase.
- Active 1-phase stays 1-phase until 3-phase would deliver more watts than the capped 1-phase offer. At the defaults, 32 A × 230 V = 7360 W on 1-phase and 11 A × 230 V × 3 = 7590 W on 3-phase, so the switch is just above that. Lowering max-1-phase-amp moves the switch down. It never forces 3-phase → 1-phase by itself.
- No active phase (first start): if 3-phase delivers more watts than 1-phase, use 3-phase. If both can offer leftover and 1-phase is still at least as good, use the preferred-start select (default 1-phase). At 6 kW that is 1-phase 26 A.
- Preferred start does not change a phase that is already running.

A wanted 1↔3 change waits `hold_minutes` in both directions. Cancelling happens when leftover again fits the phase that is actually running (the arm is cleared, which also clears the expired flag, so the next disagreement starts a fresh hold). When the hold fires, the next apply may switch. Amp is not frozen during the hold. Budget the phase that will actually run:

- Holding 1-phase while 3-phase is wanted: 1-phase leftover, capped at max-1-phase-amp.
- Holding 3-phase while 1-phase is wanted: 3-phase min amp (6 A), not the 1-phase amp you would have published.
- First start has no last `psm`, so it does not hold.

`lot = min(group_lot, max(min_amp, target_watts // (volts × phases)))`. Per-charger `amp = min(phase cap, lot)`.

Low-hold budget passes 0 W into this math. The budget function then raises the target to at least min amp × volts before choosing a phase, so a bare 0 W looks like a small 1-phase offer unless a last `psm` of 3-phase is being held. The phase hold is what keeps a running 3-phase session at 3-phase 6 A for the same hold minutes, instead of clicking 3→1 and then off. Full-power and keep do not use this hold. They write their `psm` immediately.

The initial group budget, before per-car shares, uses the single surplus charger's last `psm` when there is exactly one surplus charger. With two or more, that first budget has no last phase. Per-car phase budgets still use each serial's own last `psm`.

## 12. Group lot when someone else is at a fixed current

If any charger is full power or keep, surplus publishes `lot = group_lot` and does not publish the smaller leftover `lot`. Last writer would shrink the group and trap the 22 kW car. Surplus still publishes its own `amp`, `psm`, and `frc`. Combined demand may exceed the group (`32 A + leftover amp`, or keep amp + leftover amp). App `lop` splits the 50 A. HA does not reserve 32 A by capping surplus amp.

If nobody is full or keep, `lot` starts from total leftover (or the 6 A floor while in low hold) and is raised so the allocated amps fit, as in section 10. Every surplus setpoint in that apply shares one `lot`. Idle-armed watts are not part of that sum, except during overdraw.

## 13. Keep

Per charger, persisted: switch on/off, "seen plugged while on", phase `idle` | `allowed` | `cut`, probe-start timestamp. The switch entity is what the user sees. Internal phase can be cut while the switch is off.

Enable = the per-charger enable switch (default on) and policy is not Force off. Force off does not auto-on keep, and it does not turn off a keep that is already on.

`commanded_on` for this apply is true when the role is full power or this serial received a leftover setpoint. That is computed from the plan before keep advances, then keep may change and the plan runs again.

One step, while plugged:

- If the switch was on and is now off: phase becomes `cut` (manual off does not re-arm).
- If commanded on, plugged, and not Complete: phase becomes `allowed`.
- If not commanded, not Complete, switch is off, and phase was `allowed`: phase becomes `cut`. That is a cheap window or leftover stopping while the car is still WaitCar or Charging.
- If the switch is on: it stays on until unplug. Probe timestamp clears.
- Auto-on when the switch is off, enable is on, phase is not cut, the car is idle Complete, and it is not a steal victim. The probe timestamp starts at the first such apply. When `now − probe ≥ 60 s`, the switch turns on. A clock jump backwards restarts the probe at now. Steal-victim and not-idle clear the probe (the step returns no probe). They do not set cut.

Steal victim: leftover session is writing, and some other surplus charger is taking (≥ 100 W) at a strictly better HA priority. Equal priority is not a victim. Being a victim blocks auto-on. It is not a cut. During the 60 s probe the car is not in the keep role, so it is not in the keep pool, and leftover may be over-offered to the other car by about the idle draw.

Unplug: switch off, seen cleared, phase `idle`, probe cleared. Complete is still plugged, so a full battery does not clear keep.

Keep MQTT, when the role is keep: `psm` from the keep-phase select, `amp` from the keep-amp knob, `lot = group_lot`, `frc=2`. No phase hold. Surplus skips this serial (keep amp, not leftover amp). Its `nrg` still reduces leftover for everyone else. A later full-power role (window, Force on, until-unplug) replaces keep MQTT for that apply. The keep switch can still be on. Role order means full power wins, so the car goes to 22 kW.

Keep-cut idle Complete is offered leftover (section 10). The probe phase (`allowed` while the switch is still off) is not.

## 14. Until unplug

Persisted per charger: switch on/off, and seen.

- Switch off → off, seen cleared. Policy select is not changed.
- Switch on and plugged → on, seen.
- Switch on, not plugged, seen → off. The car unplugged after having been plugged with the switch on.
- Switch on, not plugged, not seen → stays on. It was turned on before the car was plugged.

While on, the role is full power regardless of the policy select, enough-solar, and windows. Surplus skips the serial. The policy select is left as it was for when the switch drops off.

## 15. The one command

After roles and setpoints exist, each charger gets at most one intent. `None` means publish nothing.

| Role | Intent |
| --- | --- |
| Full | on, `psm=2`, `lot=group_lot`, `amp=max_amp` |
| Keep | on, keep `psm`, `lot=group_lot`, keep `amp` |
| Surplus, session on, this serial has a setpoint | on, that setpoint's `psm` / `lot` / `amp` |
| Surplus, session on but this serial has no setpoint, or this serial just left full power, or this serial was in a leftover session | off |
| Surplus, session off, never in this leftover session, live `frc` known and not 1 | off. Neutral after an unplug would start charging |
| Surplus, session off, live `frc` unknown or already 1 | nothing. Do not spam `frc=1` at an idle SolarPriority car |
| Off (Force off, or any other non-match) | off |

Remember last leftover `psm` and `amp` only for surplus on/off. Full power and keep must not become the next phase-hold baseline. Off clears the remembered surplus phase and amp and cancels that serial's phase hold.

## 16. Publish

Live `frc`, `amp`, `lot`, `psm` are the last MQTT status payloads (integers). `nrg` updates also schedule apply. A `frc` change schedules apply. Amp/lot/psm updates are stored and used at the next apply. They do not themselves schedule one.

Compare the intent to live state:

- No intent → skip, and record it as the last command.
- Live already matches → skip, and record the intent as last. A skipped off must not leave last-command as on.
- The same intent was already published, live is still incomplete, and no present key contradicts the intent → skip and wait for the echo.
- Otherwise publish.

Contradiction: a present key disagrees (`frc` not 2 while we want on, or not 1 while we want off, or `psm`/`lot`/`amp` disagree). A missing key does not contradict. Live `frc=1` or `amp=0` after an on command is a failed start. Publish again. Do not treat it as "still waiting".

The 15-minute safety apply sets force. Force republishes the waiting-for-echo case. It does not republish a live match.

Coalesce: the first schedule in a quiet period starts a 2-second timer. Later schedules join that window and do not restart it. A flush already running defers one new 2-second window until it finishes. At flush, leftover and roles are recomputed from current sensors. Expired flags (low hold, steal hold) are sticky for that flush even if more schedules arrived while it ran.

On publish, start order is `fup`, `psm`, `lot`, `amp`, `frc`. Stop order is `frc` then `fup`.

## 17. Apply order

This is the normative order. A rewrite that shuffles it will change keep and steal.

1. Log window-active and enough-solar edges.
2. Advance until-unplug. Turning the switch off is a service call back into Home Assistant.
3. Remember whether a leftover session and which surplus amps existed.
4. Roles from current keep switches.
5. Snapshot leftover. Subtract `nrg` of keep-role chargers.
6. Surplus session decision.
7. If the session wants power and at least one charger is surplus: build setpoints (sections 10–12) and arm phase holds and offer waits.
8. Steal victims from that take map.
9. `commanded_on` = full role, or this serial has a setpoint.
10. Advance keep. This may turn the keep switch on or off.
11. If the keep on/off map changed, repeat from step 4. Do not repeat until-unplug.
12. Open or close the leftover session (section 8). Arm or cancel the low-hold timer from the decision.
13. For each charger, emit one command (section 15). Full power sets a remembered "was full" flag and cancels that serial's phase hold. Leaving full power clears the flag. Keep also cancels the phase hold.
14. If anything changed (switches, session edges, a real publish), persist and refresh entities. Otherwise refresh surplus sensors only when the surplus watts changed.

Offer-wait arming: a surplus serial that is allocated and not taking (≥ 100 W) starts a 15-second timer if one is not already running and the serial is not already expired. Taking cancels the timer and clears expired. Not allocated cancels the timer and leaves expired as it was. When the timer fires, the serial is expired and apply runs again. Expired means "this wait finished", so the car is no longer pending and steal / a further car may proceed.

Keep-probe arming: one timer for the soonest probe deadline.

## 18. What schedules apply

- Kotiakku SoC, solar, house, controller mean.
- Forecast today/tomorrow (also replans).
- Window knobs and the price-sensor text (also replans, and retargets the price subscription).
- Surplus knobs. Enough-solar and off-sun also replan.
- Policy select, until-unplug, keep, keep-enable, keep phase, keep amp, car state, priority, charger power entity.
- Price sensor changes (replan).
- MQTT `nrg` change, MQTT `frc` change.
- The 2-second flush, hold expirations, offer wait, keep probe, window boundary, 15-minute safety (replan + force).

A config-entry options update reloads the integration. Timers start over. Stored session and keep phase come back. **Accident** relative to a softer reconfigure, and it is the current behavior.

## 19. Accidents

Safe to drop in a rewrite unless you still want bug-compatible upgrades:

- `settle_s` entity and stored default. MQTT batch is 2 seconds.
- Second copy of policy names and `restore_policy`.
- `current_or_next`. Nothing in the hub calls it.
- `resolve_lop_entity_id` and the Auto / Force 1-phase / Force 3-phase select helpers. Keep phase is a two-value select.
- Stored `restore` policy map. It only feeds the one-time migration from the old until-unplug select onto the switch.
- `plugged` is passed into the allocator and ignored. Taking is `nrg` and car state.
- `pick_windows` accepts a leftover positional argument so an old `now_ts` does not shift flex. Callers should pass keywords.
- Allocation context is built twice per plan. The second build exists so steal sees offer flags the setpoint step just armed. One returned context is enough.
- Charger max watts inside allocation is always `max_amp × volts × 3`.
- The group phase seed uses only the first surplus charger's last `psm`, and only when there is a single surplus charger.
- Low-hold budgeting raises 0 W to min-amp × volts before the phase choice, then the phase hold may force 3-phase anyway.
- `strings.json` and `translations/en.json` are the same English file because the integration is English-only.
- The 48-hour dashboard reimplements enough-solar in JavaScript. It is not part of this spec.

## 20. Worked cases

These are the cases the current tests lock. A massaged spec should say which ones still hold.

- Cheapest seed of min hours, then grow to the cheaper side, stopping at flex headroom, ceiling, max hours, or an island gap.
- Seed above the ceiling → no window. A neighbor above the ceiling is not added. The clock does not replace an ended valley with a later one.
- Tomorrow follow-up only when the first window misses today 22:00 through the end of tomorrow. Abutting windows keep 22 kW on across the boundary.
- SolarPriority inside a window with enough-solar → surplus, not 22 kW. SolarAndGrid inside a window → 22 kW even when enough-solar. Force off → `frc=1` even with leftover. Force on and until-unplug → 22 kW (`psm=2`, `amp=32`, `lot=50`).
- House 300 W and EV 3 kW does not add the EV back. Unknown controller and no `nrg` → EV 0, leftover is solar − house.
- Start needs SoC ≥ 92% and leftover ≥ 2000 W. Below 1000 W or SoC < 90% holds 6 A for 15 minutes. Touching 2000 W cancels that hold. Sensors unusable cannot start, and they hold then stop.
- 12 kW leftover, high car taking 10 kW, priorities unequal → 9 kW + 3 kW after the offer wait, not during it. During the wait the taking car and the pending better car are both on (over-draw), and a third car does not start.
- 6 kW → 3 kW + 3 kW when steal is legal. 4.5 kW does not steal. Remainder ≤ 500 W does not start the next car. An already-taking second car keeps the 3 kW steal for the hold while leftover stays ≥ 6 kW.
- High priority not taking: it stays `frc=2`. The next car is first. High is stolen from or dropped only once high draws ≥ 100 W.
- Idle Complete under 400 W is skipped, unless keep-cut, in which case it is offered `frc=2`. Complete at ≥ 400 W counts as taking.
- 8 kW → 6 kW on an active 3-phase session stays 3-phase. 1-phase stays 1-phase until 3-phase would deliver more watts than max-1-phase-amp. A phase change waits 15 minutes each way. Amp still follows leftover on the phase that is running.
- First start at 6 kW with the default preferred phase is 1-phase 26 A.
- One full-power or keep charger in the group: surplus does not write a leftover `lot` below group lot.
- Keep auto-on after 60 seconds of idle Complete, enable on, not Force off, not cut, not a steal victim. Manual keep off while Complete cuts. A stop while WaitCar or Charging cuts. Unplug clears keep and until-unplug. Probe time is wall clock, not a count of applies.
