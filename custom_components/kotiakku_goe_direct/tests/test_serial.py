"""Tests for charger serial prefill (no Home Assistant)."""

from __future__ import annotations

from harness import ROOT, assert_eq, assert_true, case_runner, load_mod


def main():
    serial_mod = load_mod("serial")
    guess_serial = serial_mod.guess_serial
    extract = serial_mod.extract_guessed_serials
    looks = serial_mod.looks_like_car_state
    looks_lop = serial_mod.looks_like_lop
    looks_power = serial_mod.looks_like_charger_power
    resolve = serial_mod.resolve_car_entity_id
    resolve_lop = serial_mod.resolve_lop_entity_id
    resolve_power = serial_mod.resolve_power_entity_id
    suggestion = serial_mod.serial_suggestion
    valid = serial_mod.valid_serial

    case, run = case_runner()

    def test_extract_firmware_and_hacs():
        assert_eq(extract("go-e_111111"), ["111111"], "firmware id")
        assert_eq(extract("go-e_111111_car_state"), ["111111"], "unique_id")
        assert_eq(extract("go-eCharger/111111/status"), ["111111"], "mqtt topic")
        assert_eq(extract("111111-sensor-car_state-169"), ["111111"], "hacs unique_id")
        assert_eq(
            extract("sensor.go_echarger_111111_car_state"),
            ["111111"],
            "entity_id",
        )
        assert_eq(
            extract("sensor.go_econtroller_000000_go_e_ev_power_5_min_mean"),
            [],
            "controller entity_id skipped",
        )
        assert_eq(extract("go-eController/000000/status"), [], "controller topic")
        assert_eq(extract("sensor.nordpool_kwh_fi"), [], "nordpool")

    def test_guess_sse_and_identifiers():
        serial, source = guess_serial(attributes={"sse": "111111"})
        assert_eq(serial, "111111", "sse")
        assert_eq(source, "attr:sse", "sse source")
        serial, source = guess_serial(
            identifiers={("mqtt", "go-e_111111")},
            entity_id="sensor.garage_left",
        )
        assert_eq(serial, "111111", "identifier beats renamed entity")
        serial, source = guess_serial(
            unique_id="go-e_111111_car_state",
            entity_id="sensor.go_echarger_222222_car_state",
        )
        assert_eq(serial, "111111", "unique_id beats entity_id")
        serial, source = guess_serial(
            identifiers={("goecharger_mqtt", "111111")},
            unique_id="go-e_222222_car",
        )
        assert_eq(serial, "111111", "identifier beats unique_id")

    def test_high_conflict_is_empty():
        serial, source = guess_serial(
            identifiers={("mqtt", "go-e_111111")},
            attributes={"sse": "222222"},
        )
        assert_eq(serial, None, "conflict")
        assert_eq(source, "", "no source on conflict")

    def test_controller_entity_not_guessed():
        serial, source = guess_serial(
            entity_id="sensor.go_econtroller_000000_go_e_ev_power_5_min_mean"
        )
        assert_eq(serial, None, "no charger serial from controller")

    def test_mqtt_topic():
        serial, source = guess_serial(mqtt_topics=["go-eCharger/111111/nrg"])
        assert_eq(serial, "111111", "mqtt")
        serial, source = guess_serial(mqtt_topics=["go-eController/000000/status"])
        assert_eq(serial, None, "controller mqtt skipped")

    def test_device_serial_number():
        serial, source = guess_serial(
            serial_number="111111",
            entity_id="sensor.renamed_car",
        )
        assert_eq(serial, "111111", "device serial_number")

    def test_car_state_detection():
        assert_true(looks("sensor.go_echarger_111111_car_state"), "car_state suffix")
        assert_true(looks("sensor.go_echarger_111111_car", "go-e_111111_car"), "car key")
        assert_true(looks(None, "111111-sensor-car_state-1"), "hacs car_state")
        assert_true(not looks("sensor.go_echarger_111111_nrg"), "nrg is not car")
        assert_true(not looks("sensor.go_echarger_111111_amp"), "amp is not car")

    def test_lop_detection():
        assert_true(looks_lop("number.go_echarger_111111_lop"), "lop suffix")
        assert_true(looks_lop(None, "111111-number-lop-1"), "hacs lop")
        assert_true(looks_lop("number.go_echarger_111111_load_priority"), "load_priority")
        assert_true(not looks_lop("number.go_echarger_111111_lot"), "lot is not lop")
        assert_true(not looks_lop("number.go_echarger_111111_loty"), "loty is not lop")
        assert_true(not looks_lop("sensor.go_echarger_111111_car_state"), "car is not lop")

    def test_resolve_lop_entity():
        assert_eq(
            resolve_lop("number.go_echarger_111111_lop", "111111"),
            "number.go_echarger_111111_lop",
            "already lop",
        )
        assert_eq(
            resolve_lop(
                "sensor.go_echarger_111111_car_state",
                "111111",
                unique_id="go-e_111111_car",
                siblings=[
                    {
                        "entity_id": "number.garage_priority",
                        "unique_id": "111111-number-lop-1",
                    }
                ],
            ),
            "number.garage_priority",
            "sibling lop on same device",
        )
        assert_eq(
            resolve_lop("sensor.go_echarger_111111_car_state", "111111"),
            "number.go_echarger_111111_lop",
            "synthesized fallback",
        )

    def test_power_detection():
        assert_true(looks_power("sensor.go_echarger_111111_nrg_11"), "nrg 11")
        assert_true(looks_power("sensor.go_echarger_111111_charging_power"), "charging_power")
        assert_true(looks_power("sensor.go_echarger_111111_power"), "power suffix")
        assert_true(not looks_power("sensor.house_power"), "house is not charger")
        assert_true(not looks_power("sensor.go_echarger_111111_car_state"), "car is not power")
        assert_eq(
            resolve_power("sensor.go_echarger_111111_car_state", "111111"),
            "sensor.go_echarger_111111_nrg",
            "synthesized nrg fallback",
        )

    def test_resolve_car_entity():
        assert_eq(
            resolve("sensor.go_echarger_111111_car_state", "111111"),
            "sensor.go_echarger_111111_car_state",
            "already car_state",
        )
        assert_eq(
            resolve(
                "sensor.go_echarger_111111_nrg",
                "111111",
                unique_id="go-e_111111_nrg",
                siblings=[
                    {
                        "entity_id": "sensor.garage_car",
                        "unique_id": "go-e_111111_car",
                    }
                ],
            ),
            "sensor.garage_car",
            "sibling car on same device",
        )
        assert_eq(
            resolve("sensor.something_nrg", "111111"),
            "sensor.go_echarger_111111_car_state",
            "synthesized fallback",
        )

    def test_serial_suggestion_keeps_saved():
        prev = [{"entity": "sensor.left_car", "serial": "111111"}]
        got, how = suggestion("sensor.left_car", prev, "407499", "unique_id")
        assert_eq(got, "111111", "saved wins when entity unchanged")
        assert_eq(how, "saved", "saved")
        got, how = suggestion("sensor.right_car", prev, "222222", "unique_id")
        assert_eq(got, "222222", "new entity uses guess")
        assert_eq(how, "unique_id", "guess source")
        got, how = suggestion("sensor.right_car", prev, None)
        assert_eq(got, "", "no guess")

    def test_valid_serial():
        assert_eq(valid("111111"), "111111", "six digit")
        assert_eq(valid(" 111111 "), "111111", "strip")
        assert_eq(valid("go-e"), None, "not a serial")
        assert_eq(valid(""), None, "empty")

    case("extract_firmware_and_hacs", test_extract_firmware_and_hacs)
    case("guess_sse_and_identifiers", test_guess_sse_and_identifiers)
    case("high_conflict_is_empty", test_high_conflict_is_empty)
    case("controller_entity_not_guessed", test_controller_entity_not_guessed)
    case("mqtt_topic", test_mqtt_topic)
    case("device_serial_number", test_device_serial_number)
    case("car_state_detection", test_car_state_detection)
    case("lop_detection", test_lop_detection)
    case("resolve_car_entity", test_resolve_car_entity)
    case("resolve_lop_entity", test_resolve_lop_entity)
    case("power_detection", test_power_detection)
    case("serial_suggestion_keeps_saved", test_serial_suggestion_keeps_saved)
    case("valid_serial", test_valid_serial)

    # Load config.py with const already importable via package path trick
    import importlib.util
    import sys
    import types

    pkg = types.ModuleType("kotiakku_goe_direct")
    pkg.__path__ = [str(ROOT)]
    sys.modules.setdefault("kotiakku_goe_direct", pkg)
    const = load_mod("const")
    sys.modules["kotiakku_goe_direct.const"] = const
    sys.modules["kotiakku_goe_direct.serial"] = serial_mod
    spec = importlib.util.spec_from_file_location(
        "kotiakku_goe_direct.config", ROOT / "config.py"
    )
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)

    def test_normalize_and_form():
        rows = config.normalize_chargers(
            [
                {
                    "entity": "sensor.go_echarger_111111_car_state",
                    "serial": "111111",
                },
                {
                    "entity": "sensor.go_echarger_222222_car_state",
                    "serial": "222222",
                },
            ]
        )
        assert_eq(len(rows), 2, "two chargers")
        assert_eq(rows[0]["priority"], 1, "default first priority")
        assert_eq(rows[1]["priority"], 2, "default second priority")
        form = config.form_from_chargers(rows)
        assert_eq(form["charger_1_serial"], "111111", "form serial")
        assert_eq(form["charger_1_priority"], 1, "form priority")
        back = config.chargers_from_form(form)
        assert_eq(back[0]["serial"], "111111", "round trip")
        assert_eq(back[0]["priority"], 1, "priority round trip")
        err = config.validate_charger_rows(back)
        assert_eq(err, None, "valid")
        one = config.normalize_chargers(
            [
                {
                    "entity": "sensor.go_echarger_111111_car_state",
                    "serial": "111111",
                }
            ]
        )
        assert_eq(len(one), 1, "one charger")
        assert_eq(config.validate_charger_rows(one), None, "one charger valid")
        custom = config.normalize_chargers(
            [
                {
                    "entity": "sensor.go_echarger_111111_car_state",
                    "serial": "111111",
                    "priority": 50,
                },
                {
                    "entity": "sensor.go_echarger_222222_car_state",
                    "serial": "222222",
                    "priority": 10,
                },
            ]
        )
        assert_eq(custom[0]["priority"], 50, "custom first")
        assert_eq(custom[1]["priority"], 10, "custom second")
        assert_eq(config.clamp_priority(0, 1), 1, "clamp low")
        assert_eq(config.clamp_priority(100, 1), 99, "clamp high")
        assert_eq(const.default_charger_priority(0), 1, "slot 0")
        assert_eq(const.default_charger_priority(3), 4, "slot 3")
        assert_eq(
            const.priority_entity_id("111111"),
            "number.kotiakku_goe_direct_priority_111111",
            "priority entity",
        )
        skipped = config.chargers_from_form(
            {
                "charger_1_entity": "sensor.go_echarger_111111_car_state",
                "charger_1_serial": "111111",
                "charger_1_priority": 1,
                "charger_2_entity": "",
                "charger_2_serial": "",
                "charger_3_entity": "sensor.go_echarger_222222_car_state",
                "charger_3_serial": "222222",
                "charger_3_priority": 7,
            }
        )
        assert_eq(len(skipped), 2, "empty slot 2 omitted")
        assert_eq(skipped[1]["serial"], "222222", "slot 3 kept")
        assert_eq(skipped[1]["priority"], 7, "slot 3 priority")
        assert_eq(
            config.validate_charger_rows(
                [{"entity": "sensor.x", "serial": "111111"}] * 2
            ),
            "duplicate_serial",
            "dup",
        )
        assert_eq(
            config.validate_charger_rows([{"entity": "sensor.x", "serial": ""}]),
            "serial_required",
            "serial required",
        )
        strings = config.normalize_chargers(["111111"])
        assert_eq(strings[0]["serial"], "111111", "string serial")
        assert_eq(strings[0]["priority"], 1, "string serial default priority")
        assert_eq(
            strings[0]["entity"],
            "sensor.go_echarger_111111_car_state",
            "synthesized entity",
        )
        stored = config.persistable(
            {
                "chargers": [
                    {
                        "entity": "sensor.go_echarger_111111_car_state",
                        "serial": "111111",
                        "priority": 8,
                    }
                ]
            }
        )
        assert_eq(stored["chargers"][0]["priority"], 8, "persist priority")
        assert_eq(config.normalize_chargers(None), [], "no default chargers")
        assert_eq(config.normalize_chargers([]), [], "empty list")
        guessed = config.apply_serial_guesses(
            [
                {"entity": "sensor.right", "serial": "111111", "priority": 20},
                {"entity": "sensor.left", "serial": "", "priority": 30},
            ],
            [
                {"entity": "sensor.left", "serial": "111111", "priority": 1},
                {"entity": "sensor.right", "serial": "222222", "priority": 2},
            ],
            {"sensor.right": "222222", "sensor.left": "111111"},
        )
        assert_eq(guessed[0]["serial"], "222222", "stale serial replaced")
        assert_eq(guessed[1]["serial"], "111111", "empty serial filled")
        assert_eq(guessed[0]["priority"], 20, "priority kept on guess")
        assert_eq(guessed[1]["priority"], 30, "priority kept on fill")

    def test_entry_config_options_overlay():
        class Entry:
            data = {"price_entity": "sensor.a", "controller_entity": "sensor.old"}
            options = {"controller_entity": "sensor.new", "soc_on": 90}

        cfg = config.entry_config(Entry())
        assert_eq(cfg["price_entity"], "sensor.a", "data kept")
        assert_eq(cfg["controller_entity"], "sensor.new", "options overlay")
        assert_eq(cfg["soc_on"], 90, "soc overlay")
        assert_eq(cfg["controller_in_kw"], False, "default controller_in_kw")
        assert_eq(cfg["soc_entity"], "", "no invented soc")
        assert_eq(cfg["solar_entity"], "", "no invented solar")
        assert_eq(cfg["house_entity"], "", "no invented house")
        assert_eq(cfg["chargers"], [], "no invented chargers")

    def test_persistable_does_not_invent_entities():
        cfg = config.persistable({})
        assert_eq(cfg["controller_entity"], "", "empty controller")
        assert_eq(cfg["soc_entity"], "", "empty soc")
        assert_eq(cfg["solar_entity"], "", "empty solar")
        assert_eq(cfg["house_entity"], "", "empty house")
        assert_eq(cfg["price_entity"], "", "empty price")
        assert_eq(cfg["chargers"], [], "empty chargers")
        assert_eq(cfg["kotiakku_in_kw"], True, "kotiakku still kW")
        assert_eq(cfg["controller_in_kw"], False, "controller still watts")
        assert_eq(cfg["soc_on"], 92, "default soc on")
        assert_eq(cfg["start_min_w"], 2000, "default start leftover")
        assert_eq(cfg["hold_min_w"], 1000, "default low hold leftover")
        picked = config.persistable(
            {
                "controller_entity": "sensor.my_ev_mean",
                "soc_entity": "sensor.my_soc",
            }
        )
        assert_eq(picked["controller_entity"], "sensor.my_ev_mean", "keeps pick")
        assert_eq(picked["soc_entity"], "sensor.my_soc", "keeps soc")
        assert_eq(picked["house_entity"], "", "does not fill house")
        assert_eq(picked["solar_today_entity"], "", "does not invent today")
        assert_eq(picked["solar_tomorrow_entity"], "", "does not invent tomorrow")

    def test_source_refresh_ids():
        ids = config.source_refresh_ids(
            (
                "sensor.soc",
                "sensor.pv",
                "sensor.house",
                "sensor.ev",
                "sensor.energy_production_today",
                "sensor.energy_production_tomorrow",
                "",
                None,
            ),
            (
                "sensor.nordpool_kwh_fi",
                "sensor.energy_production_today",
                "sensor.go_echarger_111111_car_state",
                "  ",
            ),
        )
        assert_eq(
            ids,
            [
                "sensor.soc",
                "sensor.pv",
                "sensor.house",
                "sensor.ev",
                "sensor.energy_production_today",
                "sensor.energy_production_tomorrow",
                "sensor.nordpool_kwh_fi",
                "sensor.go_echarger_111111_car_state",
            ],
            "unique wired sources in order",
        )
        assert_eq(config.source_refresh_ids(), [], "no groups")
        assert_eq(config.source_refresh_ids(None, []), [], "empty groups")

    def test_persistable_legacy_solar_keys():
        migrated = config.persistable(
            {
                "solar_forecast_remaining_entity": "sensor.energy_production_today_remaining",
                "solar_forecast_tomorrow_entity": "sensor.energy_production_tomorrow",
                "supercheap_min_kwh": 50,
            }
        )
        assert_eq(
            migrated["solar_today_entity"],
            "sensor.energy_production_today_remaining",
            "legacy remaining key maps to solar today",
        )
        assert_eq(
            migrated["solar_tomorrow_entity"],
            "sensor.energy_production_tomorrow",
            "legacy tomorrow",
        )
        assert_eq(migrated["solar_enough_kwh"], 50, "legacy enough kWh")
        assert_eq("supercheap_min_kwh" in migrated, False, "legacy key not stored")
        assert_eq(
            "solar_forecast_remaining_entity" in migrated,
            False,
            "legacy remaining key is not stored",
        )
        from_remaining = config.persistable(
            {"solar_remaining_entity": "sensor.energy_production_today_remaining"}
        )
        assert_eq(
            from_remaining["solar_today_entity"],
            "sensor.energy_production_today_remaining",
            "solar_remaining_entity maps to solar today",
        )
        assert_eq(
            "solar_remaining_entity" in from_remaining,
            False,
            "solar_remaining_entity is not stored",
        )
        prefer = config.persistable(
            {
                "solar_today_entity": "sensor.energy_production_today",
                "solar_remaining_entity": "sensor.old_remaining",
                "solar_enough_kwh": 30,
                "supercheap_min_kwh": 50,
            }
        )
        assert_eq(prefer["solar_today_entity"], "sensor.energy_production_today", "today key wins")
        assert_eq("solar_remaining_entity" in prefer, False, "old remaining key is not stored")
        assert_eq(prefer["solar_enough_kwh"], 30, "new enough wins")

    def test_psm_and_surplus_eids():
        assert_eq(const.psm_option(0), "Auto", "0 Auto")
        assert_eq(const.psm_option(1), "Force 1-phase", "1")
        assert_eq(const.psm_option(2), "Force 3-phase", "2")
        assert_eq(const.psm_option("x"), "Auto", "bad")
        assert_eq(const.psm_int("Force 1-phase"), 1, "option to int")
        assert_eq(const.psm_int("nope"), 0, "unknown option")
        spec_ids = [spec["entity_id"] for spec in const.SURPLUS_NUMBER_SPECS]
        assert_eq(len(spec_ids), 15, "fifteen surplus numbers")
        for entity_id in spec_ids:
            assert_true(entity_id in const.SURPLUS_EIDS, entity_id)
        assert_true(const.EID_ECO_PSM in const.SURPLUS_EIDS, "eco psm")
        assert_true(const.EID_HOLD_MIN_W in const.SURPLUS_EIDS, "hold leftover")
        assert_true(const.EID_SPLIT_MIN_W in const.SURPLUS_EIDS, "split leftover")
        assert_true(const.EID_SPLIT_FLOOR_W in const.SURPLUS_EIDS, "remainder floor")
        assert_true(const.EID_SOLAR_ENOUGH_KWH in const.SURPLUS_EIDS, "enough solar")
        assert_true(const.EID_OFFSUN_HOUR_KWH in const.SURPLUS_EIDS, "offsun hour")
        assert_eq(const.DEFAULT_SPLIT_MIN_W, 3000, "next surplus min 3 kW")
        assert_eq(const.DEFAULT_SPLIT_FLOOR_W, 500, "remainder floor 500 W")
        assert_eq(const.DEFAULT_SOLAR_ENOUGH_KWH, 40, "enough solar 40 kWh")
        assert_eq(const.DEFAULT_OFFSUN_HOUR_KWH, 1, "offsun hour 1 kWh")
        assert_eq(config.persistable({})["split_min_w"], 3000, "persist split min")
        assert_eq(config.persistable({})["split_floor_w"], 500, "persist remainder floor")
        assert_eq(config.persistable({})["solar_enough_kwh"], 40, "persist enough solar")
        assert_eq(config.persistable({})["offsun_hour_kwh"], 1, "persist offsun hour")
        assert_eq(const.POLICIES, ("SolarPriority", "Force on", "Force off"), "three policies")
        assert_eq("Supercheap" in const.POLICIES, False, "Supercheap is not a live policy")
        assert_eq("Force on until unplug" in const.POLICIES, False, "until unplug is not a policy")
        assert_eq(const.POLICY_UNTIL_UNPLUG, "Force on until unplug", "legacy until unplug name")
        assert_eq(const.restore_policy("Cheapest"), "SolarPriority", "legacy Cheapest")
        assert_eq(const.restore_policy("Supercheap"), "SolarPriority", "legacy Supercheap")
        assert_eq(const.restore_policy("Longest"), "SolarPriority", "legacy Longest")
        assert_eq(const.restore_policy("Earliest"), "SolarPriority", "legacy Earliest")
        assert_eq(const.DEFAULT_CEILING, 0.2, "new ceiling default")
        assert_eq(const.DEFAULT_FLEX_PCT, 20.0, "flex percent default")
        assert_eq(const.DEFAULT_FLEX_EUR, 0.02, "flex euro default")
        assert_eq(
            const.until_unplug_entity_id("111111"),
            "switch.kotiakku_goe_direct_until_unplug_111111",
            "until unplug switch",
        )
        assert_eq(const.DOMAIN, "kotiakku_goe_direct", "domain")
        assert_eq(const.HUB_ID, "kotiakku_goe_direct", "hub id")
        assert_eq(const.STORAGE_KEY, "kotiakku_goe_direct", "storage key")
        assert_eq(const.EID_MIN, "number.kotiakku_goe_direct_window_min_h", "window min hours")
        assert_eq(const.EID_MAX, "number.kotiakku_goe_direct_window_max_h", "window max hours")
        assert_eq(const.EID_FLEX_PCT, "number.kotiakku_goe_direct_window_flex_pct", "flex percent")
        assert_eq(const.EID_FLEX_EUR, "number.kotiakku_goe_direct_window_flex_eur", "flex euro")
        assert_eq(const.EID_WINDOW, "sensor.kotiakku_goe_direct_window", "window sensor")
        assert_eq(const.EID_SOLAR_TODAY_KWH, "sensor.kotiakku_goe_direct_solar_today_kwh", "solar today")
        assert_eq(const.EID_SOLAR_TOMORROW_KWH, "sensor.kotiakku_goe_direct_solar_tomorrow_kwh", "solar tomorrow")
        assert_eq(const.EID_SOLAR_GATING_KWH, "sensor.kotiakku_goe_direct_solar_gating_kwh", "solar gating")
        assert_eq(const.EID_SOLAR_GATING_DAY, "sensor.kotiakku_goe_direct_solar_gating_day", "gating day")
        assert_eq(const.SOLAR_TODAY_UNIQUE_ID, "kotiakku_goe_direct_solar_today_kwh", "today unique id")
        assert_eq(const.SOLAR_TOMORROW_UNIQUE_ID, "kotiakku_goe_direct_solar_tomorrow_kwh", "tomorrow unique id")
        assert_eq(const.SOLAR_GATING_KWH_UNIQUE_ID, "kotiakku_goe_direct_solar_gating_kwh", "gating kWh unique id")
        assert_eq(const.SOLAR_GATING_DAY_UNIQUE_ID, "kotiakku_goe_direct_solar_gating_day", "gating day unique id")
        assert_eq(
            const.EID_WINDOW_ACTIVE,
            "binary_sensor.kotiakku_goe_direct_window_active",
            "window active",
        )
        assert_true(const.EID_FLEX_PCT in const.WINDOW_EIDS, "flex pct is a window eid")
        assert_true(const.EID_FLEX_EUR in const.WINDOW_EIDS, "flex euro is a window eid")

    def test_window_sensor_registry_migration():
        class Registry:
            def __init__(self, rows):
                self.by_uid = {}
                self.by_eid = {}
                for row in rows:
                    self.by_uid[(row["domain"], row["platform"], row["unique_id"])] = row["entity_id"]
                    self.by_eid[row["entity_id"]] = dict(row)
                self.updates = []
                self.removed = []

            def async_get_entity_id(self, domain, platform, unique_id):
                return self.by_uid.get((domain, platform, unique_id))

            def async_get(self, entity_id):
                return self.by_eid.get(entity_id)

            def async_update_entity(self, entity_id, **kwargs):
                self.updates.append((entity_id, dict(kwargs)))
                row = self.by_eid[entity_id]
                old_uid = row["unique_id"]
                new_uid = kwargs.get("new_unique_id", old_uid)
                new_eid = kwargs.get("new_entity_id", entity_id)
                del self.by_uid[(row["domain"], row["platform"], old_uid)]
                del self.by_eid[entity_id]
                row = dict(row)
                row["unique_id"] = new_uid
                row["entity_id"] = new_eid
                self.by_uid[(row["domain"], row["platform"], new_uid)] = new_eid
                self.by_eid[new_eid] = row

            def async_remove(self, entity_id):
                self.removed.append(entity_id)
                row = self.by_eid.pop(entity_id, None)
                if row is None:
                    return
                self.by_uid.pop((row["domain"], row["platform"], row["unique_id"]), None)

        offsun = Registry(
            [
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_offsun_window",
                    "entity_id": "sensor.kotiakku_goe_direct_offsun_window",
                },
                {
                    "domain": "binary_sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_offsun_window_active",
                    "entity_id": "binary_sensor.kotiakku_goe_direct_offsun_window_active",
                },
            ]
        )
        const.migrate_window_entities(offsun)
        assert_eq(
            offsun.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_window"),
            "sensor.kotiakku_goe_direct_window",
            "offsun sensor renamed to window",
        )
        assert_eq(
            offsun.async_get_entity_id(
                "binary_sensor", const.DOMAIN, "kotiakku_goe_direct_window_active"
            ),
            "binary_sensor.kotiakku_goe_direct_window_active",
            "offsun active renamed",
        )
        assert_eq(
            offsun.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_offsun_window"),
            None,
            "old offsun unique_id gone",
        )
        start_legacy = Registry(
            [
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_offsun_window_start",
                    "entity_id": "sensor.kotiakku_goe_direct_offsun_window_start",
                }
            ]
        )
        const.migrate_window_entities(start_legacy)
        assert_eq(
            start_legacy.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_window"),
            "sensor.kotiakku_goe_direct_window",
            "legacy _window_start renamed to window",
        )
        ranks = Registry(
            [
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_cheapest_window",
                    "entity_id": "sensor.kotiakku_goe_direct_cheapest_window",
                },
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_longest_window_start",
                    "entity_id": "sensor.kotiakku_goe_direct_longest_window_start",
                },
                {
                    "domain": "binary_sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_earliest_window_active",
                    "entity_id": "binary_sensor.kotiakku_goe_direct_earliest_window_active",
                },
            ]
        )
        const.migrate_window_entities(ranks)
        assert_eq(
            ranks.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_cheapest_window"),
            None,
            "cheapest sensor deleted",
        )
        assert_eq(
            ranks.async_get_entity_id(
                "sensor", const.DOMAIN, "kotiakku_goe_direct_longest_window_start"
            ),
            None,
            "longest leftover start deleted",
        )
        assert_eq(
            ranks.async_get_entity_id(
                "binary_sensor", const.DOMAIN, "kotiakku_goe_direct_earliest_window_active"
            ),
            None,
            "earliest active deleted",
        )
        assert_true(
            "sensor.kotiakku_goe_direct_cheapest_window" in ranks.removed,
            "cheapest row removed",
        )
        collision = Registry(
            [
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_offsun_window",
                    "entity_id": "sensor.kotiakku_goe_direct_offsun_window",
                },
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_window",
                    "entity_id": "sensor.kotiakku_goe_direct_window",
                },
            ]
        )
        const.migrate_window_entities(collision)
        assert_eq(
            collision.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_offsun_window"),
            None,
            "collision removes the offsun row",
        )
        assert_eq(
            collision.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_window"),
            "sensor.kotiakku_goe_direct_window",
            "existing window unique_id kept",
        )
        assert_true(
            "sensor.kotiakku_goe_direct_offsun_window" in collision.removed,
            "offsun removed on collision",
        )
        custom = Registry(
            [
                {
                    "domain": "sensor",
                    "platform": const.DOMAIN,
                    "unique_id": "kotiakku_goe_direct_offsun_window",
                    "entity_id": "sensor.my_offsun_plan",
                }
            ]
        )
        const.migrate_window_entities(custom)
        assert_eq(
            custom.async_get_entity_id("sensor", const.DOMAIN, "kotiakku_goe_direct_window"),
            "sensor.my_offsun_plan",
            "custom entity_id kept on rename",
        )
        fresh = Registry([])
        const.migrate_window_entities(fresh)
        assert_eq(fresh.updates, [], "new install has nothing to migrate")
        assert_eq(fresh.removed, [], "new install removes nothing")
        assert_eq(
            const.EID_CEILING,
            "number.kotiakku_goe_direct_electricity_price_ceiling",
            "electricity price ceiling",
        )
        assert_eq(const.EID_PRICE, "text.kotiakku_goe_direct_electricity_price_sensor", "electricity price sensor")
        assert_eq(const.EID_SOC_ON, "number.kotiakku_goe_direct_soc_on_pct", "soc on pct")
        assert_eq(const.EID_HOLD_MIN, "number.kotiakku_goe_direct_hold_minutes", "hold minutes")
        assert_eq(const.EID_VOLTS, "number.kotiakku_goe_direct_voltage_v", "voltage")
        assert_eq(const.EID_MIN_AMP, "number.kotiakku_goe_direct_min_a", "min amp")
        assert_eq(const.EID_MAX_AMP, "number.kotiakku_goe_direct_max_a", "max amp")
        assert_eq(const.EID_ECO_LOT, "number.kotiakku_goe_direct_eco_lot_a", "eco lot")
        assert_eq(const.EID_ECO_PSM, "select.kotiakku_goe_direct_eco_phase", "eco phase")
        assert_eq(
            const.EID_SOLAR_ENOUGH_KWH,
            "number.kotiakku_goe_direct_solar_enough_kwh",
            "enough solar kWh",
        )
        assert_eq(
            const.EID_OFFSUN_HOUR_KWH,
            "number.kotiakku_goe_direct_offsun_hour_kwh",
            "offsun hour kWh",
        )
        assert_eq(const.EID_START_MIN_W, "number.kotiakku_goe_direct_surplus_start_w", "surplus start")
        assert_eq(
            const.EID_SPLIT_MIN_W,
            "number.kotiakku_goe_direct_next_surplus_min_w",
            "next surplus min",
        )
        assert_eq(
            const.EID_SPLIT_FLOOR_W,
            "number.kotiakku_goe_direct_remainder_floor_w",
            "remainder floor",
        )
        assert_eq(const.EID_HOLD_MIN_W, "number.kotiakku_goe_direct_low_hold_w", "low hold leftover")
        for spec in const.SURPLUS_NUMBER_SPECS:
            assert_true(
                spec["unique_id"].startswith("kotiakku_goe_direct_"),
                "unique_id %s" % spec["unique_id"],
            )
            assert_eq(
                config.INT_KEYS[spec["conf"]],
                spec["default"],
                "INT_KEYS matches %s" % spec["conf"],
            )
        assert_eq(config.INT_KEYS[const.CONF_ECO_PSM], const.DEFAULT_ECO_PSM, "eco psm persist")

    case("normalize_and_form", test_normalize_and_form)
    case("entry_config_options_overlay", test_entry_config_options_overlay)
    case("persistable_does_not_invent_entities", test_persistable_does_not_invent_entities)
    case("source_refresh_ids", test_source_refresh_ids)
    case("persistable_legacy_solar_keys", test_persistable_legacy_solar_keys)
    case("psm_and_surplus_eids", test_psm_and_surplus_eids)
    case("window_sensor_registry_migration", test_window_sensor_registry_migration)

    run()


if __name__ == "__main__":
    main()
