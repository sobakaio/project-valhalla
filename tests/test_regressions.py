import copy
import concurrent.futures
import threading
import time
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import server as app
import qa_preview_audit as preview_qa


def director_fields(payload):
    return {
        field["key"]: field
        for group in payload["groups"]
        for field in group["fields"]
    }


class StudioGenerationLimitTests(unittest.TestCase):
    def test_run_config_accepts_counts_above_the_previous_limits(self):
        database, _ = app.load_database()
        config = app.parse_run_config(
            {"mode": "photoshoot", "count": 201, "photoshoots": 51}, database
        )
        self.assertEqual(config.count, 201)
        self.assertEqual(config.photoshoots, 51)

    def test_run_config_still_rejects_non_positive_counts(self):
        database, _ = app.load_database()
        with self.assertRaisesRegex(app.AppError, "Images must be at least 1"):
            app.parse_run_config({"count": 0}, database)

    def test_studio_number_inputs_have_no_maximum(self):
        html = (Path(app.__file__).parent / "client" / "client.html").read_text(encoding="utf-8")
        self.assertNotIn('name="photoshoots" min="1" max=', html)
        self.assertNotIn('name="count" min="1" max=', html)

    def test_content_mode_rejects_obsolete_boolean_flags(self):
        database, _ = app.load_database()
        with self.assertRaisesRegex(app.AppError, "Obsolete content configuration"):
            app.parse_run_config({"xxx_only": True}, database)

    def test_content_mode_accepts_only_current_values(self):
        database, _ = app.load_database()
        for mode in ("sfw", "progressive", "xxx"):
            self.assertEqual(
                app.parse_run_config({"content_mode": mode}, database).content_mode,
                mode,
            )
        with self.assertRaisesRegex(app.AppError, "content_mode"):
            app.parse_run_config({"content_mode": "legacy"}, database)


class CatalogQualityTests(unittest.TestCase):
    def test_every_bra_and_panties_template_uses_one_exact_color_group(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(20260724))
        checked = 0
        for template in database["outfit_templates"]:
            if not {"bra", "panties"}.issubset(template["slots"]):
                continue
            group = template["slots"]["bra"].get("color_group")
            self.assertTrue(group, template["id"])
            self.assertEqual(
                group, template["slots"]["panties"].get("color_group"),
                template["id"],
            )
            for _ in range(8):
                outfit = composer.choose_outfit(template)
                if not {"bra", "panties"}.issubset(outfit["garments"]):
                    continue
                self.assertEqual(
                    outfit["colors"]["bra"]["id"],
                    outfit["colors"]["panties"]["id"],
                    template["id"],
                )
                checked += 1
        self.assertGreater(checked, 100)
        broken = copy.deepcopy(database)
        template = next(
            item for item in broken["outfit_templates"]
            if {"bra", "panties"}.issubset(item["slots"])
        )
        template["slots"]["panties"].pop("color_group", None)
        with self.assertRaisesRegex(app.AppError, "shared color_group"):
            app.validate_database(broken)

    def test_breast_traits_define_safe_covered_and_exposed_anchors(self):
        database, _ = app.load_database()
        for section in ("breast_size", "breast_shape"):
            for item in database["human_model_parts"][section]:
                self.assertTrue(item["prompt"].strip())
                self.assertTrue(item["covered_prompt"].strip())
                for exposed_term in ("breast", "nipple", "areola", "bust"):
                    self.assertNotIn(exposed_term, item["covered_prompt"].casefold())
        broken = copy.deepcopy(database)
        broken["human_model_parts"]["breast_size"][0].pop("covered_prompt")
        with self.assertRaisesRegex(app.AppError, "covered_prompt"):
            app.validate_database(broken)

    def test_location_alternatives_are_separate_concrete_entities(self):
        database, _ = app.load_database()
        self.assertNotIn("semantic_prompt_audit", database["settings"])
        interior = next(
            item for item in database["interiors"]
            if item["id"] == "interior_modern_bedroom"
        )
        bed = next(
            item for item in database["furniture"]
            if item["id"] == "furniture_bed"
        )
        candidates = app.matching_location_zones(database, interior, bed)
        self.assertEqual(
            {item["id"] for item in candidates},
            {"zone_on_bed", "zone_beside_bed"},
        )
        observed = {
            app.resolve_location_zone(
                database, interior, bed, app.random.Random(seed)
            )["id"]
            for seed in range(40)
        }
        self.assertEqual(observed, {"zone_on_bed", "zone_beside_bed"})

    def test_sportswear_template_pairs_sporty_tops_and_pants(self):
        database, _ = app.load_database()
        template = next(
            item for item in database["outfit_templates"]
            if item["id"] == "template_sportswear_set"
        )
        composer = app.Composer(database, app.random.Random(62026))
        observed_tops = set()
        observed_bottoms = set()
        for _ in range(120):
            outfit = composer.choose_outfit(template)
            top = outfit["garments"]["upperwear"]
            bottom = outfit["garments"]["lowerwear"]
            self.assertIn("sporty", app.tags(top))
            self.assertIn("sporty", app.tags(bottom))
            observed_tops.add(top["id"])
            observed_bottoms.add(bottom["id"])
        self.assertGreaterEqual(len(observed_tops), 3)
        self.assertGreaterEqual(len(observed_bottoms), 4)

    def test_tshirts_are_automatically_reachable_in_default_catalog(self):
        database, _ = app.load_database()
        tshirts = {
            "top_boxy_tshirt", "top_crewneck_tshirt",
            "top_vneck_tshirt", "top_longsleeve_tshirt",
        }
        reachability = app.catalog_reachability(database)
        self.assertTrue(tshirts.issubset(reachability["automatic"]["garments"]))

    def test_dressed_panties_reveal_is_probable_and_structurally_integrated(self):
        database, _ = app.load_database()
        rule = database["settings"]["dressed_panties_reveal"]
        stage = {
            "id": "test_dressed", "level": "covered",
            "visible_slots": ["upperwear", "lowerwear"], "body_visibility": [],
        }
        outfit = {"garments": {
            "lowerwear": {"id": rule["compatible_outer_ids"][0]},
            "panties": {"id": "panties_test"},
        }}
        rng = app.random.Random(90210)
        selected = sum(
            app.maybe_dressed_panties_reveal(
                database, stage, outfit, rng
            ).get("visual_category") == "dressed_panties_reveal"
            for _ in range(2000)
        )
        self.assertGreater(selected, 280)
        self.assertLess(selected, 440)

        configured = copy.deepcopy(database)
        configured["settings"]["dressed_panties_reveal"]["chance"] = 1
        reveals = []
        for seed in range(30):
            run = app.parse_run_config({
                "mode": "random", "content_mode": "progressive", "count": 12,
                "photoshoots": 1, "prompt_seed": seed,
                "inference_seed": seed + 1, "nsfw_percent": 50,
                "plateau_percent": 20,
            }, configured)
            scene_rng = app.random.Random(seed)
            board = app.build_storyboard(
                run, configured, app.Composer(configured, scene_rng), scene_rng,
                run.nsfw_percent, run.plateau_percent,
            )
            reveals = [
                shot for shot in board
                if shot["stage"].get("visual_category")
                == "dressed_panties_reveal"
            ]
            if reveals:
                break
        self.assertTrue(reveals)
        for shot in reveals:
            self.assertIn("panties", shot["stage"]["visible_slots"])
            self.assertIn(
                "dressed_panties_reveal_action", app.tags(shot["scene"]["action"])
            )
            positive, _, _ = app.compile_scene(configured, shot["scene"])
            self.assertIn(rule["positive_prompt"], positive)

        composer = app.Composer(database, app.random.Random(717171))
        for _ in range(1000):
            fixed = composer.fixed_context()
            probe = {"context": fixed}
            options = app.director_stage_options(database, probe, "progressive")
            reveal_options = [
                item for item in options
                if item.get("visual_category") == "dressed_panties_reveal"
            ]
            if reveal_options:
                break
        self.assertTrue(reveal_options)
        self.assertIn("panties", reveal_options[0]["visible_slots"])

    def test_xxx_camera_recipes_produce_broad_non_macro_compositions(self):
        database, _ = app.load_database()
        observed = {
            key: Counter() for key in ("shot_size", "camera_angle", "framing")
        }
        for seed in range(100):
            run = app.parse_run_config({
                "mode": "photoshoot", "content_mode": "xxx", "count": 12,
                "photoshoots": 1, "prompt_seed": seed, "inference_seed": seed + 1,
            }, database)
            rng = app.random.Random(seed)
            board = app.build_storyboard(
                run, database, app.Composer(database, rng), rng,
                run.nsfw_percent, run.plateau_percent,
            )
            for shot in board:
                scene = shot["scene"]
                if scene["stage"]["level"] == "explicit":
                    for key in observed:
                        observed[key][scene[key]["id"]] += 1
        total = observed["shot_size"].total()
        self.assertGreater(total, 0)
        self.assertGreaterEqual(len(observed["shot_size"]), 5)
        self.assertGreaterEqual(len(observed["camera_angle"]), 5)
        self.assertGreaterEqual(len(observed["framing"]), 4)
        self.assertLess(observed["shot_size"]["shot_intimate_macro"] / total, 0.1)
        self.assertLess(observed["shot_size"]["shot_rear_closeup"] / total, 0.35)

    def test_validate_cli_is_read_only_and_does_not_start_the_server(self):
        report = {
            "templates": 1, "interiors": 1, "garments": 1,
            "outfit_checks": 1, "sfw_outfit_checks": 1, "stage_checks": 1,
            "storyboard_checks": 1, "compiled_scenes": 1,
            "camera_checks": 10_000, "camera_tuples": 1,
            "explicit_recipes": 1, "reachable_records": 1, "warnings": [],
        }
        with (
            patch.object(app.sys, "argv", ["server.py", "validate"]),
            patch.object(app, "load_config", return_value=({}, Path("config.json"))),
            patch.object(app, "load_database", return_value=({}, Path("database.json"))),
            patch.object(app, "validate_production_catalog", return_value=report) as validate,
            patch.object(app, "serve") as serve,
            patch("builtins.print") as output,
        ):
            self.assertEqual(app.main(), 0)
        validate.assert_called_once_with({})
        serve.assert_not_called()
        self.assertTrue(any(
            call.args and call.args[0] == "Valhalla production validation passed"
            for call in output.call_args_list
        ))

    def test_production_validation_rejects_an_unreachable_enabled_garment(self):
        database, _ = app.load_database()
        broken = copy.deepcopy(database)
        broken["garments"]["upperwear"].append({
            "id": "top_validation_orphan",
            "catalog_category": "normal",
            "prompt": "validation-only unreachable woven top",
            "tags": ["validation_orphan"],
            "allowed_colors": ["color_black"],
        })
        with self.assertRaisesRegex(
            app.AppError, "upperwear.top_validation_orphan"
        ):
            app.validate_production_catalog(broken)

    def test_validate_is_an_explicit_cli_command(self):
        args = app.build_parser().parse_args(["validate"])
        self.assertEqual(args.command, "validate")

    def test_stats_is_an_explicit_cli_command(self):
        args = app.build_parser().parse_args(["stats"])
        self.assertEqual(args.command, "stats")

    def test_exact_catalog_analysis_reaches_every_enabled_record(self):
        database, _ = app.load_database()
        analysis = app.catalog_reachability(database)
        self.assertEqual(analysis["unreachable"], {})
        self.assertEqual(
            analysis["reachable"]["garments"], analysis["enabled"]["garments"]
        )
        self.assertEqual(
            analysis["reachable"]["human_traits"],
            analysis["enabled"]["human_traits"],
        )
        self.assertEqual(
            analysis["reachable"]["poses"], analysis["enabled"]["poses"]
        )
        self.assertEqual(
            analysis["reachable"]["actions"], analysis["enabled"]["actions"]
        )
        stats = app.catalog_statistics(database, analysis)
        self.assertEqual(stats["unreachable"], {})
        self.assertEqual(stats["sections"]["garments"], 437)
        self.assertGreater(len(stats["pool_summaries"]["garment_slot"]["narrow"]), 0)

    def test_slavic_human_defaults_are_universal_except_explicit_constraints(self):
        database, _ = app.load_database()
        pools = database["settings"]["human_defaults"]["pools"]
        self.assertNotIn("hair_style_half_up", pools["hair_style"])
        self.assertEqual(pools["ethnic_appearance"], ["appearance_slavic"])
        self.assertEqual(
            set(pools["breast_size"]),
            {"breasts_very_small", "breasts_small", "breasts_small_full"},
        )
        self.assertEqual(pools["manicure"], ["manicure_none"])
        self.assertEqual(pools["body_state"], ["body_state_none"])
        self.assertEqual(
            set(pools["pubic_hair"]), {"pubic_shaved", "pubic_full_natural"}
        )
        self.assertEqual(
            set(pools["hair_color"]),
            {
                "hair_brown", "hair_blonde", "hair_auburn",
                "hair_platinum", "hair_dark_brown", "hair_chestnut",
                "hair_copper", "hair_strawberry_blonde", "hair_ash_blonde",
                "hair_blue_black",
            },
        )
        self.assertEqual(pools["makeup"], ["makeup_no_makeup"])
        self.assertEqual(
            set(pools["hair_length"]),
            {"hair_length_shoulder", "hair_length_long", "hair_length_very_long"},
        )
        self.assertNotIn("hair_style_side_swept", pools["hair_style"])
        self.assertFalse({"hair_style_pixie", "hair_style_bob", "hair_style_lob"} & set(pools["hair_style"]))
        self.assertEqual(
            set(pools["body_frame"]),
            {"body_slender", "body_average", "body_athletic", "body_petite", "body_fitness", "body_lithe"},
        )
        constrained = {
            "ethnic_appearance", "hair_color", "manicure", "breast_size",
            "pubic_hair", "body_state", "makeup", "hair_length", "hair_style",
            "body_frame",
        }
        self.assertTrue(all(not values for key, values in pools.items() if key not in constrained))

        slavic = next(
            item for item in database["human_model_parts"]["ethnic_appearance"]
            if item["id"] == "appearance_slavic"
        )
        for category in (
            "face_shape", "eye_shape", "eye_color", "eyebrows", "nose",
            "lips", "hair_texture",
        ):
            self.assertTrue(all(
                app.compatible_with_requirements(item, app.tags(slavic))
                for item in database["human_model_parts"][category]
                if not item.get("disabled", False)
            ), category)

        humans = [
            app.Composer(database, app.random.Random(seed)).choose_human()
            for seed in range(250)
        ]
        observed_lengths = {human["hair_length"]["id"] for human in humans}
        self.assertEqual(
            observed_lengths,
            {"hair_length_shoulder", "hair_length_long", "hair_length_very_long"},
        )
        self.assertEqual(
            {human["body_state"]["id"] for human in humans},
            {"body_state_none"},
        )

    def test_classical_ballet_is_a_reachable_complete_luxury_outfit(self):
        database, _ = app.load_database()
        template = next(
            item for item in database["outfit_templates"]
            if item["id"] == "template_classical_ballet"
        )
        self.assertEqual(template["catalog_category"], "luxury")
        outfit = app.Composer(database, app.random.Random(20260721)).choose_outfit(template)
        self.assertEqual(outfit["garments"]["full_body"]["id"], "dress_ballet_tutu")
        self.assertEqual(outfit["garments"]["legwear"]["id"], "legwear_ballet_tights")
        self.assertEqual(outfit["garments"]["footwear"]["id"], "shoes_ballet_pointe")
        ballet_color_family = app.color_family(
            database, outfit["colors"]["full_body"]["id"]
        )
        self.assertEqual(
            ballet_color_family,
            app.color_family(database, outfit["colors"]["bra"]["id"]),
        )
        self.assertEqual(
            ballet_color_family,
            app.color_family(database, outfit["colors"]["panties"]["id"]),
        )
        self.assertEqual(
            [stage["id"] for stage in template["stages"]],
            ["ballet_dressed", "ballet_lingerie", "ballet_topless"],
        )

    def test_custom_mast_actions_are_wired_as_hands_only_actions(self):
        database, _ = app.load_database()
        actions = {item["id"]: item for item in database["actions"]}
        expected_hands = {
            "action_inserting_vagina": 1,
            "action_inserting_anus": 1,
            "action_inserting_both": 2,
        }
        for action_id, hands in expected_hands.items():
            item = actions[action_id]
            self.assertTrue(item["prompt"].strip())
            self.assertTrue(item["menu_label"].strip())
            self.assertTrue(
                {"explicit_action", "masturbation_action", "insertion_action", "explicit"}.issubset(
                    app.tags(item)
                )
            )
            self.assertEqual(set(item["requires_tags"]), {"genitals", "open_legs"})
            self.assertEqual(item["hands_required"], hands)

        recipe = next(
            item for item in database["explicit_recipes"]
            if item["id"] == "recipe_insertion_play"
        )
        self.assertEqual(recipe["action_tags"], ["insertion_action"])
        self.assertEqual(recipe["shot_size"], "shot_three_quarter")

        for seed in range(25):
            args = app.parse_run_config({
                "mode": "photoshoot", "count": 12, "photoshoots": 1,
                "prompt_seed": seed, "content_mode": "xxx",
            }, database)
            rng = app.random.Random(seed)
            storyboard = app.build_storyboard(
                args, database, app.Composer(database, rng), rng, 100, 100
            )
            insertion_shots = [
                shot for shot in storyboard
                if (shot["scene"].get("explicit_recipe") or {}).get("id")
                == "recipe_insertion_play"
            ]
            self.assertTrue(insertion_shots)
            self.assertTrue(all(
                "insertion_action" in app.tags(shot["scene"]["action"])
                for shot in insertion_shots
            ))
            self.assertEqual(item["allowed_levels"], ["explicit"])

    def test_database_rejects_duplicate_json_keys(self):
        with self.assertRaisesRegex(app.AppError, "Duplicate JSON key: catalog_category"):
            app.json.loads(
                '{"catalog_category":"normal","catalog_category":"luxury"}',
                object_pairs_hook=app.unique_json_object,
            )

    def test_database_source_contains_no_duplicate_json_keys(self):
        source = Path(app.__file__).with_name("database.json").read_text(encoding="utf-8")
        app.json.loads(source, object_pairs_hook=app.unique_json_object)

    def test_intimate_arousal_modifiers_are_visibility_gated_and_seeded(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(20260721))
        context = composer.fixed_context()
        explicit = next(
            stage
            for stage in app.effective_photoshoot_stages(context["outfit"]["template"])
            if stage["level"] == "explicit"
        )
        intimate = composer.resolve_scene(
            context, explicit, {"explicit_recipe": "recipe_intimate_macro"}
        )
        masturbation = composer.resolve_scene(
            context, explicit, {"explicit_recipe": "recipe_hands_only"}
        )
        rear = composer.resolve_scene(
            context, explicit, {"explicit_recipe": "recipe_rear_standing"}
        )
        breast = composer.resolve_scene(
            context, explicit, {"explicit_recipe": "recipe_breast_focus"}
        )
        modifier = intimate["intimate_arousal_modifier"]
        self.assertIn(
            modifier["id"],
            {item["id"] for item in database["intimate_arousal_modifiers"]},
        )
        self.assertIn(modifier["prompt"], app.compile_scene(database, intimate)[0])
        self.assertIsNotNone(masturbation["intimate_arousal_modifier"])
        self.assertIsNone(rear["intimate_arousal_modifier"])
        self.assertIsNone(breast["intimate_arousal_modifier"])

    def test_wardrobe_environment_and_surface_catalogs_use_one_explicit_category(self):
        database, _ = app.load_database()
        items = list(database["outfit_templates"])
        items.extend(database["interiors"])
        items.extend(database["furniture"])
        items.extend(
            item for values in database["garments"].values() for item in values
        )
        self.assertGreater(len(items), 500)
        self.assertTrue(all(item.get("catalog_category") in {"normal", "luxury"} for item in items))
        self.assertFalse(any("wardrobe_category" in item for item in items))

    def test_scene_default_categories_reliably_isolate_normal_and_luxury(self):
        database, _ = app.load_database()
        for category in ("normal", "luxury"):
            configured = copy.deepcopy(database)
            defaults = configured["settings"]["scene_defaults"]
            defaults["wardrobe_categories"] = [category]
            defaults["environment_categories"] = [category]
            app.validate_database(configured)
            selected_garment_categories = set()
            selected_surface_categories = set()
            for seed in range(60):
                context = app.Composer(configured, app.random.Random(seed)).fixed_context()
                self.assertEqual(app.catalog_category(context["outfit"]["template"]), category)
                self.assertEqual(app.catalog_category(context["interior"]), category)
                self.assertTrue(app.category_allows(context["interior"], context["furniture"]))
                selected_surface_categories.add(app.catalog_category(context["furniture"]))
                for garment in context["outfit"]["garments"].values():
                    self.assertTrue(app.category_allows(context["outfit"]["template"], garment))
                    selected_garment_categories.add(app.catalog_category(garment))
            if category == "normal":
                self.assertEqual(selected_garment_categories, {"normal"})
                self.assertEqual(selected_surface_categories, {"normal"})
            else:
                self.assertIn("luxury", selected_garment_categories)
                self.assertIn("luxury", selected_surface_categories)

    def test_enabling_both_categories_cycles_through_both_in_a_batch(self):
        database, _ = app.load_database()
        defaults = database["settings"]["scene_defaults"]
        self.assertEqual(set(defaults["wardrobe_categories"]), {"normal", "luxury"})
        self.assertEqual(set(defaults["environment_categories"]), {"normal", "luxury"})
        composer = app.Composer(database, app.random.Random(20260721))
        contexts = [composer.fixed_context() for _ in range(10)]
        self.assertEqual(
            [app.catalog_category(item["outfit"]["template"]) for item in contexts].count("luxury"),
            5,
        )
        self.assertEqual(
            [app.catalog_category(item["interior"]) for item in contexts].count("luxury"),
            5,
        )

    def test_photoshoot_keeps_surface_style_per_furniture_family(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(606060))
        fixed = composer.fixed_context()
        stages = app.effective_photoshoot_stages(fixed["outfit"]["template"])
        scenes = [
            composer.resolve_scene(fixed, stages[index % len(stages)])
            for index in range(20)
        ]
        family_tags = ("bed", "sofa", "chair", "rug", "floor", "wall")
        styles = {}
        checked = 0
        for scene in scenes:
            family = next((
                tag for tag in family_tags if tag in app.tags(scene["furniture"])
            ), None)
            if not family:
                continue
            style = (
                (scene.get("surface_color") or {}).get("id"),
                (scene.get("surface_texture") or {}).get("id"),
            )
            if family in styles:
                self.assertEqual(style, styles[family])
                checked += 1
            styles[family] = style
        self.assertGreater(checked, 0)

    def test_teddy_bears_have_multiple_authored_plausible_colors(self):
        database, _ = app.load_database()
        teddy_bears = [
            item for item in database["props"]
            if item["id"].startswith("prop_teddy_bear")
        ]
        self.assertEqual(len(teddy_bears), 4)
        prompts = " ".join(item["prompt"].casefold() for item in teddy_bears)
        for color in ("brown", "cream", "dusty-pink", "dove-gray"):
            self.assertIn(color, prompts)
        self.assertTrue(all("playful_prop" in app.tags(item) for item in teddy_bears))

    def test_requested_gestures_and_intimate_spread_have_real_hand_budgets(self):
        database, _ = app.load_database()
        actions = {item["id"]: item for item in database["actions"]}
        finger = actions["action_finger_tip_in_mouth"]
        middle = actions["action_middle_finger_to_viewer"]
        spread = actions["action_spread_intimate_anatomy"]
        self.assertEqual(
            set(finger["allowed_levels"]),
            {"covered", "lingerie", "topless", "nude", "explicit"},
        )
        self.assertEqual(app.hands_required(finger), 1)
        self.assertEqual(app.hands_required(middle), 1)
        self.assertEqual(app.hands_required(spread), 1)
        self.assertIn("index and middle fingers", spread["prompt"])
        self.assertIn("uncovered vulva", spread["prompt"])
        self.assertTrue({"genitals", "open_legs"}.issubset(spread["requires_tags"]))

        both_hands_pose = next(
            item for item in database["poses"]
            if "both hands" in item["prompt"].casefold()
        )
        self.assertEqual(app.hands_required(both_hands_pose), 2)
        self.assertGreater(app.hands_required(both_hands_pose) + app.hands_required(finger), 2)

    def test_expanded_adult_action_set_has_structural_clothing_and_hand_rules(self):
        database, _ = app.load_database()
        actions = {item["id"]: item for item in database["actions"]}
        expected_hands = {
            "action_waistband_tease": 1,
            "action_panties_side_pull": 1,
            "action_cover_bare_breasts": 2,
            "action_squeeze_one_breast": 1,
            "action_pinch_one_nipple": 1,
            "action_pull_bra_cup_aside": 1,
            "action_hold_unbuttoned_shirt_open": 2,
            "action_hand_between_thighs": 1,
            "action_cup_over_panties": 1,
            "action_rub_over_panties": 1,
            "action_touch_clitoris": 1,
            "action_trace_vulva": 1,
            "action_insert_two_fingers": 1,
            "action_spread_and_clitoral_touch": 2,
            "action_breast_and_intimate_touch": 2,
            "action_spread_buttocks": 2,
            "action_pull_panties_between_cheeks": 1,
            "action_taste_fingertips": 1,
            "action_inviting_finger": 1,
            "action_blow_kiss_bare_breast": 1,
        }
        self.assertTrue(set(expected_hands).issubset(actions))
        for action_id, required in expected_hands.items():
            item = actions[action_id]
            self.assertEqual(app.hands_required(item), required, action_id)
            self.assertTrue(item.get("menu_label", "").strip(), action_id)
            self.assertTrue(item["prompt"].strip(), action_id)

        for action_id in ("action_waistband_tease", "action_cup_over_panties", "action_rub_over_panties"):
            self.assertIn("panties", actions[action_id]["requires_tags"])
            self.assertIn("genitals", actions[action_id]["excludes_tags"])
        self.assertTrue(
            {"bra", "breasts", "nipples"}.issubset(
                actions["action_pull_bra_cup_aside"]["requires_tags"]
            )
        )
        self.assertIn(
            "braless_required",
            actions["action_hold_unbuttoned_shirt_open"]["excludes_tags"],
        )
        self.assertIn(
            "provocative_rear",
            actions["action_pull_panties_between_cheeks"]["requires_tags"],
        )
        self.assertIn(
            "insertion_action", app.tags(actions["action_insert_two_fingers"])
        )

    def test_hand_budget_fallback_understands_legacy_pose_action_and_prop_text(self):
        self.assertEqual(app.hands_required({"prompt": "resting both hands at her waist"}), 2)
        self.assertEqual(app.hands_required({"prompt": "tracing one hand along her hip"}), 1)
        self.assertEqual(app.hands_required({"prompt": "holding a teddy bear"}), 1)
        self.assertEqual(
            app.hands_required({"prompt": "holding a teddy bear", "hands_required": 2}),
            2,
        )

    def test_structural_partial_undressing_precedes_complete_removal(self):
        database, _ = app.load_database()
        expected = {
            83: ("lowerwear", "lowered_to_hips", "lowered to the hips"),
            430: ("upperwear", "unbuttoned_open", "front panels hanging open"),
        }
        for seed, (slot, state, prompt_fragment) in expected.items():
            args = app.parse_run_config({
                "mode": "photoshoot", "count": 12, "photoshoots": 1,
                "prompt_seed": seed, "content_mode": "progressive",
            }, database)
            rng = app.random.Random(seed)
            shots = app.build_storyboard(
                args, database, app.Composer(database, rng), rng,
                args.nsfw_percent, args.plateau_percent,
            )
            position = next(
                index for index, shot in enumerate(shots)
                if shot["scene"].get("garment_states", {}).get(slot) == state
            )
            partial = shots[position]
            self.assertIn(slot, partial["stage"]["visible_slots"])
            positive, _, _ = app.compile_scene(database, partial["scene"])
            self.assertIn(prompt_fragment, positive)
            later = shots[position + 1:]
            removed_at = next(
                shot for shot in later if slot in shot["scene"]["removed_garment_slots"]
            )
            self.assertNotIn(slot, removed_at["stage"].get("visible_slots", []))
            self.assertTrue(
                removed_at["scene"]["garment_transition"]["prompt"].startswith(
                    "fully removed and no longer wearing"
                )
            )

    def test_removed_shot_transition_options_never_claim_half_removed_state(self):
        database, _ = app.load_database()
        scene = {
            "garment_transition": {
                "id": "transition_lowerwear", "prompt": "removed", "slots": ["lowerwear"],
            },
            "outfit": {
                "garments": {"lowerwear": {"prompt": "blue jeans"}},
            },
        }
        options = app.director_transition_options(scene)
        self.assertTrue(options)
        self.assertFalse(any("half" in item["prompt"].casefold() for item in options))

    def test_tan_lines_are_probable_fixed_traits_with_visibility_gating(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(919191))
        selected = [composer.choose_human()["skin_marking"] for _ in range(1000)]
        marked = [item for item in selected if item["id"] != "skin_marking_none"]
        self.assertGreater(len(marked), 160)
        self.assertLess(len(marked), 240)

        combined = next(
            item for item in database["human_model_parts"]["skin_marking"]
            if item["id"] == "skin_marking_bra_panty_tan_lines"
        )
        human = composer.choose_human()
        human["skin_marking"] = combined
        human["skin_tone"] = next(
            item for item in database["human_model_parts"]["skin_tone"]
            if item["id"] == "skin_fair"
        )
        bra_prompt = combined["bra_line_prompt_by_skin"]["light_skin"]
        panty_prompt = combined["panty_line_prompt_by_skin"]["light_skin"]
        covered = " ".join(app.human_fragments(human, set(), {}))
        chest = " ".join(app.human_fragments(human, {"breasts", "nipples"}, {}))
        lower = " ".join(app.human_fragments(human, {"pubic_area", "genitals"}, {}))
        both = " ".join(app.human_fragments(
            human, {"breasts", "nipples", "pubic_area", "genitals"}, {}
        ))
        self.assertNotIn("tan lines", covered)
        self.assertIn(bra_prompt, chest)
        self.assertNotIn(panty_prompt, chest)
        self.assertIn(panty_prompt, lower)
        self.assertNotIn(bra_prompt, lower)
        self.assertIn(bra_prompt, both)
        self.assertIn(panty_prompt, both)

        neutral = next(
            item for item in database["human_model_parts"]["skin_marking"]
            if item["id"] == "skin_marking_none"
        )
        human["skin_marking"] = neutral
        self.assertNotIn("tan lines", " ".join(app.human_fragments(human, {
            "breasts", "nipples", "pubic_area", "genitals",
        }, {})))

    def test_skin_marking_schema_requires_separate_gated_components(self):
        database, _ = app.load_database()
        broken = copy.deepcopy(database)
        broken["human_model_parts"]["skin_marking"][0].pop("bra_line_prompt")
        with self.assertRaisesRegex(app.AppError, "bra_line_prompt"):
            app.validate_database(broken)

    def test_skin_marking_tone_variants_require_all_skin_classes(self):
        database, _ = app.load_database()
        broken = copy.deepcopy(database)
        marked = next(
            item for item in broken["human_model_parts"]["skin_marking"]
            if item.get("bra_line_prompt_by_skin")
        )
        marked["bra_line_prompt_by_skin"].pop("dark_skin")
        with self.assertRaisesRegex(app.AppError, "bra_line_prompt_by_skin"):
            app.validate_database(broken)

    def test_age_catalog_contains_only_three_explicit_adult_presets(self):
        database, _ = app.load_database()
        ages = database["human_model_parts"]["age"]
        self.assertEqual(
            [(item["id"], item["prompt"]) for item in ages],
            [
                ("girl_21", "21-year-old girl"),
                ("girl_22", "22-year-old girl"),
                ("girl_23", "23-year-old girl"),
            ],
        )
        self.assertTrue(all("adult" in app.tags(item) for item in ages))

    def test_catalog_has_large_unique_curated_collections(self):
        database, _ = app.load_database()
        self.assertGreaterEqual(sum(1 for _ in app.iter_content_items(database)), 1100)
        self.assertGreaterEqual(len(database["interiors"]), 60)
        self.assertGreaterEqual(len(database["poses"]), 100)
        self.assertGreaterEqual(len(database["actions"]), 79)
        self.assertGreaterEqual(
            sum(len(items) for items in database["garments"].values()), 430
        )
        ids = {item["id"] for item in app.iter_content_items(database)}
        self.assertIn("interior_apartment_compact_bedroom", ids)
        self.assertIn("interior_apartment_studio_flat", ids)
        self.assertIn("shoes_simple_01", ids)
        self.assertIn("action_curated_20", ids)

    def test_catalog_has_no_mechanical_variants_and_modifier_refs_are_valid(self):
        database, _ = app.load_database()
        items = list(app.iter_content_items(database))
        self.assertFalse(any(item.get("expansion_source") for item in items))
        self.assertFalse(any(item.get("expansion_variant") for item in items))
        garment_ids = {
            item["id"] for values in database["garments"].values() for item in values
        }
        for modifier in database["patterns"] + database["fabric_textures"]:
            allowed = set(modifier["allowed_garment_ids"])
            self.assertTrue(allowed)
            self.assertTrue(allowed.issubset(garment_ids))
        pattern_garments = set().union(*(
            set(item["allowed_garment_ids"]) for item in database["patterns"]
        ))
        texture_garments = set().union(*(
            set(item["allowed_garment_ids"])
            for item in database["fabric_textures"]
        ))
        self.assertGreaterEqual(len(pattern_garments), 200)
        self.assertGreaterEqual(len(texture_garments), 220)
        self.assertGreaterEqual(
            sum(item_id.startswith("panties_") for item_id in pattern_garments), 20
        )
        self.assertGreaterEqual(
            sum("surface_texture_target" in item for item in database["furniture"]),
            20,
        )

    def test_catalog_prompts_are_unique_and_model_friendly(self):
        database, _ = app.load_database()
        prompts = [
            item["prompt"].strip().casefold()
            for item in app.iter_content_items(database)
            if item.get("prompt")
        ]
        self.assertEqual(len(prompts), len(set(prompts)))
        self.assertLessEqual(max(len(prompt.split()) for prompt in prompts), 48)
        self.assertFalse(any("production variation" in prompt for prompt in prompts))

    def test_base_negative_prompt_contains_only_minimal_anatomy_terms(self):
        database, _ = app.load_database()
        negative = database["prompt_defaults"]["negative_prompt"]
        self.assertEqual(
            negative,
            "extra limbs, missing limbs, fused limbs, malformed hands, extra fingers, "
            "fused fingers, malformed feet, extra toes, malformed anatomy, distorted face",
        )
        for removed in (
            "man", "male", "dildo", "vibrator", "sex toy", "multiple people",
            "low quality", "blurry", "watermark", "text",
        ):
            self.assertNotIn(removed, negative.split(", "))


class DirectorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.profile_registry = patch.object(
            app, "load_workflow_profile_registry",
            return_value={"production": "test-model", "preview": "test-model"},
        )
        self.profile_registry.start()
        self.addCleanup(self.profile_registry.stop)

    def make_storyboard(self, **overrides):
        state = app.WebState()
        config = {
            "mode": "photoshoot",
            "count": 4,
            "photoshoots": 1,
            "prompt_seed": 1234,
            "inference_seed": 5678,
        }
        config.update(overrides)
        board = state.create_storyboard(config)
        return state, board["id"]

    def test_all_outfit_compatible_stage_levels_are_available_even_for_full_xxx(self):
        state, storyboard_id = self.make_storyboard(content_mode="xxx")
        fields = director_fields(state.director_payload(storyboard_id, 1))
        levels = {option["prompt"] for option in fields["shot.stage"]["options"]}
        template = state.get_storyboard(storyboard_id)["shots"][0]["context"]["outfit"]["template"]
        expected = {
            stage["level"] for stage in app.effective_photoshoot_stages(template)
        }
        self.assertTrue(expected.issubset(levels))
        self.assertIn("explicit", levels)

    def test_sfw_only_resolves_fully_covered_scenes_in_both_modes(self):
        blocked = app.SFW_BLOCKED_VISIBILITY
        for mode in ("photoshoot", "random"):
            state = app.WebState()
            board = state.create_storyboard({
                "mode": mode,
                "content_mode": "sfw",
                "count": 64,
                "photoshoots": 1,
                "prompt_seed": 20260721,
                "inference_seed": 42,
            })
            record = state.get_storyboard(board["id"])
            self.assertEqual(board["config"]["content_mode"], "sfw")
            self.assertIsNone(board["config"]["nsfw_percent"])
            for shot in record["shots"]:
                self.assertTrue(app.is_sfw_stage(shot["stage"]))
                self.assertEqual(shot["stage"]["level"], "covered")
                self.assertFalse(blocked & set(shot["stage"].get("body_visibility", [])))
                app.validate_sfw_outfit(shot["context"]["outfit"])
                visible_slots = set(shot["stage"].get("visible_slots", []))
                for slot, garment in shot["context"]["outfit"]["garments"].items():
                    if slot in visible_slots:
                        self.assertFalse(app.tags(garment) & app.SFW_BLOCKED_GARMENT_TAGS)
                self.assertIsNone(shot["scene"].get("explicit_recipe"))
                self.assertIn(shot["scene"]["intensity"], {"fashion", "sensual"})
                self.assertFalse(
                    app.tags(shot["scene"]["pose"]) & app.SFW_BLOCKED_DIRECTION_TAGS
                )
                self.assertFalse(
                    app.tags(shot["scene"]["action"]) & app.SFW_BLOCKED_DIRECTION_TAGS
                )

    def test_sfw_rejects_erotic_cutout_swimwear_and_strengthens_coverage_negative(self):
        database, _ = app.load_database()
        garment = next(
            item for item in database["garments"]["full_body"]
            if item["id"] == "swimsuit_cutout"
        )
        self.assertIn("erotic", app.tags(garment))
        self.assertTrue(app.tags(garment) & app.SFW_BLOCKED_GARMENT_TAGS)
        negative = database["prompt_defaults"]["negative_profiles"]["covered_opaque"]
        for phrase in ("missing top", "absent upper garment", "breasts outside clothing"):
            self.assertIn(phrase, negative)

    def test_sfw_director_exposes_only_covered_stages_and_safe_intensity(self):
        state, storyboard_id = self.make_storyboard(content_mode="sfw")
        fields = director_fields(state.director_payload(storyboard_id, 1))
        self.assertEqual(
            {option["prompt"] for option in fields["shot.stage"]["options"]},
            {"covered"},
        )
        self.assertEqual(
            {option["id"] for option in fields["shot.intensity"]["options"]},
            {"fashion", "sensual"},
        )

    def test_sfw_director_lists_only_realizable_outfit_and_direction_options(self):
        state, storyboard_id = self.make_storyboard(
            content_mode="sfw", count=4, prompt_seed=1101
        )
        fields = director_fields(state.director_payload(storyboard_id, 1))
        database = state.get_storyboard(storyboard_id)["db"]
        listed_templates = {
            option["id"] for option in fields["outfit.template"]["options"]
        }
        self.assertEqual(listed_templates, {
            template["id"] for template in database["outfit_templates"]
            if not template.get("disabled", False)
            and app.template_supports_sfw(database, template)
        })
        for option in fields["outfit.template"]["options"]:
            fresh, fresh_id = self.make_storyboard(
                content_mode="sfw", count=4, prompt_seed=1101
            )
            fresh.update_director(fresh_id, {
                "shot": 1, "field": "outfit.template", "value": option["id"],
            })
        shot = state.get_storyboard(storyboard_id)["shots"][0]
        for key, section in (
            ("shot.pose", "poses"), ("shot.action", "actions"),
            ("shot.expression", "expressions"),
        ):
            index = {item["id"]: item for item in database[section]}
            for option in fields[key]["options"]:
                item = index[option["id"]]
                self.assertTrue(app.item_allows_intensity(
                    item, shot["scene"]["intensity"]
                ))
                if key != "shot.expression":
                    self.assertFalse(
                        app.tags(item) & app.SFW_BLOCKED_DIRECTION_TAGS
                    )
        with self.assertRaisesRegex(app.AppError, "SFW only"):
            state.update_director(storyboard_id, {
                "shot": 1, "field": "shot.intensity", "value": "explicit",
            })
        record = state.get_storyboard(storyboard_id)
        topless = next(
            stage for stage in app.effective_photoshoot_stages(
                record["shots"][0]["context"]["outfit"]["template"]
            )
            if stage["level"] == "topless"
        )
        with self.assertRaisesRegex(app.AppError, "Unknown stage"):
            state.update_director(storyboard_id, {
                "shot": 1, "field": "shot.stage", "value": topless["id"],
            })

    def test_sfw_import_rejects_noncovered_stage_and_obsolete_config(self):
        state, storyboard_id = self.make_storyboard(content_mode="xxx")
        exported = state.export_storyboard(storyboard_id)
        exported["config"]["content_mode"] = "sfw"
        with self.assertRaisesRegex(app.AppError, "not allowed in SFW only mode"):
            state.import_storyboard(exported)
        exported["config"].pop("content_mode")
        with self.assertRaisesRegex(app.AppError, "missing its content mode"):
            state.import_storyboard(exported)

    def test_camera_grammar_is_visible_and_editable_in_director(self):
        state, storyboard_id = self.make_storyboard()
        payload = state.director_payload(storyboard_id, 1)
        fields = director_fields(payload)
        for key in (
            "shot.furniture", "shot.editorial_role", "shot.shot_size",
            "shot.camera_angle", "shot.framing", "shot.focus_target",
        ):
            self.assertIn(key, fields)
            self.assertTrue(fields[key]["options"])

    def test_pregnancy_is_a_persistent_body_modifier_without_waist_conflict(self):
        state, storyboard_id = self.make_storyboard()
        fields = director_fields(state.director_payload(storyboard_id, 1))
        modifier = fields["human.body_state"]
        self.assertEqual(modifier["label"], "Body modifier")
        self.assertEqual(
            {option["label"] for option in modifier["options"]},
            {"No modifier", "Pregnant"},
        )

        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.body_state",
                "value": "body_state_pregnant",
            },
        )
        record = state.get_storyboard(storyboard_id)
        rendered = state.storyboard_payload(record)["shots"]
        for shot, payload in zip(record["shots"], rendered):
            self.assertEqual(
                shot["context"]["human"]["body_state"]["id"],
                "body_state_pregnant",
            )
            self.assertIn("pregnant", payload["positive_prompt"].casefold())
            self.assertNotIn(
                shot["context"]["human"]["waist"]["prompt"],
                payload["positive_prompt"],
            )
            positive = payload["positive_prompt"].casefold()
            lingerie_marker = (
                "revealing lingerie composition"
                if {"breasts", "nipples"} & set(shot["stage"].get("body_visibility", []))
                else "one fully opaque chest-covering lingerie garment"
            )
            stage_marker = {
                "covered": "fully opaque upper-body garment",
                "lingerie": lingerie_marker,
                "topless": "topless",
                "nude": "fully nude body",
                "explicit": (
                    shot["scene"]["explicit_recipe"]["prompt"].casefold()
                    if shot["scene"].get("explicit_recipe")
                    else "explicit adult pose"
                ),
            }[shot["stage"]["level"]]
            self.assertLess(positive.index("pregnant"), positive.index(stage_marker))
            self.assertLess(
                positive.index("pregnant"),
                positive.index(shot["context"]["human"]["ethnic_appearance"]["prompt"].casefold()),
            )

    def test_default_photoshoot_stays_in_casual_amateur_treatment_pools(self):
        database, _ = app.load_database()
        pools = database["settings"]["scene_defaults"]["pools"]
        self.assertEqual(pools["interiors"], [])
        self.assertEqual(pools["furniture"], [])
        allowed = {
            "mood": set(pools["moods"]),
            "photography_style": (
                set(pools["photography_styles"])
                | set(pools["explicit_photography_styles"])
            ),
        }
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(count=12, prompt_seed=seed)
            for shot in state.get_storyboard(storyboard_id)["shots"]:
                scene = shot["scene"]
                for field in ("mood", "photography_style"):
                    self.assertIn(scene[field]["id"], allowed[field], (seed, field))

    def test_compiled_prompt_contains_only_current_frame_visual_instructions(self):
        state, storyboard_id = self.make_storyboard(count=12)
        prompts = [
            shot["positive_prompt"]
            for shot in state.storyboard_payload(
                state.get_storyboard(storyboard_id)
            )["shots"]
        ]
        temporal_placeholders = (
            "clothing continuity",
            "clothing state remains consistent",
            "wearing the listed",
            "listed retained",
        )
        self.assertFalse(
            any(term in prompt for prompt in prompts for term in temporal_placeholders)
        )
        self.assertFalse(any("complete intact body" in prompt for prompt in prompts))
        self.assertFalse(
            any("two legs" in prompt or "two feet" in prompt for prompt in prompts)
        )
        explicit_negatives = [
            shot["negative_prompt"].split(", ")
            for shot in state.storyboard_payload(
                state.get_storyboard(storyboard_id)
            )["shots"]
            if shot["stage"]["level"] == "explicit"
        ]
        self.assertTrue(explicit_negatives)
        self.assertTrue(
            all("clothes" not in terms and "underwear" not in terms for terms in explicit_negatives)
        )

    def test_director_camera_options_are_unique_real_presets(self):
        state, storyboard_id = self.make_storyboard()
        fields = director_fields(state.director_payload(storyboard_id, 1))
        options = fields["shot.camera_angle"]["options"]
        self.assertEqual(len(options), len({item["id"] for item in options}))
        self.assertEqual(len(options), len({item["prompt"] for item in options}))

    def test_surface_color_and_texture_are_composed_and_director_editable(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=0)
        record = state.get_storyboard(storyboard_id)
        position = next(
            index for index, shot in enumerate(record["shots"], 1)
            if shot["scene"]["furniture"].get("surface_texture_target")
        )
        payload = state.director_payload(storyboard_id, position)
        fields = director_fields(payload)
        self.assertIn("shot.surface_color", fields)
        self.assertIn("shot.surface_texture", fields)
        color = next(
            option for option in fields["shot.surface_color"]["options"]
            if option["id"] and option["id"] != fields["shot.surface_color"]["value"]
        )
        texture = next(
            option for option in fields["shot.surface_texture"]["options"]
            if option["id"] and option["id"] != fields["shot.surface_texture"]["value"]
        )
        state.update_director(
            storyboard_id,
            {"shot": position, "field": "shot.surface_color", "value": color["id"]},
        )
        state.update_director(
            storyboard_id,
            {"shot": position, "field": "shot.surface_texture", "value": texture["id"]},
        )
        rendered = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"][position - 1]
        self.assertIn(color["prompt"], rendered["positive_prompt"])
        self.assertIn(texture["prompt"], rendered["positive_prompt"])
        imported = state.import_storyboard(state.export_storyboard(storyboard_id))
        imported_shot = state.get_storyboard(imported["id"])["shots"][position - 1]["scene"]
        self.assertEqual(imported_shot["surface_color"]["id"], color["id"])
        self.assertEqual(imported_shot["surface_texture"]["id"], texture["id"])

    def test_amateur_style_does_not_reduce_explicit_direction(self):
        poses, actions, recipes, plateau_kinds = set(), set(), set(), set()
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(
                count=12, prompt_seed=seed, content_mode="xxx"
            )
            for shot in state.get_storyboard(storyboard_id)["shots"]:
                scene = shot["scene"]
                self.assertEqual(scene["stage"]["level"], "explicit")
                self.assertIn("explicit_pose", app.tags(scene["pose"]))
                self.assertIn("explicit_action", app.tags(scene["action"]))
                poses.add(scene["pose"]["id"])
                actions.add(scene["action"]["id"])
                plateau_kinds.add(scene["stage"].get("plateau_kind"))
                if scene.get("explicit_recipe"):
                    recipes.add(scene["explicit_recipe"]["id"])
        self.assertGreaterEqual(len(poses), 35)
        self.assertGreaterEqual(len(actions), 30)
        self.assertGreaterEqual(len(recipes), 8)
        self.assertEqual(
            plateau_kinds,
            {"provocative_rear", "intimate_closeup", "masturbation"},
        )

    def test_full_xxx_uses_concrete_seeded_recipe_arc_with_peak_finale(self):
        state, storyboard_id = self.make_storyboard(
            count=20, prompt_seed=774411, content_mode="xxx"
        )
        shots = state.get_storyboard(storyboard_id)["shots"]
        recipes = [shot["scene"]["explicit_recipe"] for shot in shots]
        self.assertTrue(all(recipe is not None for recipe in recipes))
        self.assertTrue(all(
            shot["stage"]["planned_recipe_id"] == recipe["id"]
            for shot, recipe in zip(shots, recipes)
        ))
        self.assertTrue(all(recipe["intensity"] == "explicit" for recipe in recipes[:-1]))
        self.assertEqual(recipes[-1]["intensity"], "peak")
        self.assertEqual(shots[-1]["scene"]["editorial_role"]["id"], "role_peak")
        self.assertTrue(all(
            left["id"] != right["id"]
            for left, right in zip(recipes, recipes[1:])
        ))

        database = state.get_storyboard(storyboard_id)["db"]
        explicit_ids = {
            item["id"] for item in database["explicit_recipes"]
            if not item.get("disabled", False) and item.get("intensity") != "peak"
        }
        first_bag = {recipe["id"] for recipe in recipes[:len(explicit_ids)]}
        self.assertEqual(first_bag, explicit_ids)

        second_state, second_id = self.make_storyboard(
            count=20, prompt_seed=774411, content_mode="xxx"
        )
        second = [
            shot["scene"]["explicit_recipe"]["id"]
            for shot in second_state.get_storyboard(second_id)["shots"]
        ]
        self.assertEqual([recipe["id"] for recipe in recipes], second)

    def test_full_xxx_outfit_change_preserves_planned_recipe_arc(self):
        state, storyboard_id = self.make_storyboard(
            count=12, prompt_seed=880022, content_mode="xxx"
        )
        before = [
            shot["stage"]["planned_recipe_id"]
            for shot in state.get_storyboard(storyboard_id)["shots"]
        ]
        fields = director_fields(state.director_payload(storyboard_id, 1))
        alternative = next(
            option["id"] for option in fields["outfit.template"]["options"]
            if option["id"] != fields["outfit.template"]["value"]
        )
        state.update_director(storyboard_id, {
            "shot": 1, "field": "outfit.template", "value": alternative,
        })
        shots = state.get_storyboard(storyboard_id)["shots"]
        self.assertEqual(
            [shot["stage"]["planned_recipe_id"] for shot in shots], before
        )
        self.assertTrue(all(
            shot["scene"]["explicit_recipe"]["id"] == planned
            for shot, planned in zip(shots, before)
        ))

    def test_progressive_intensity_is_monotonic_and_recipe_compatible(self):
        order = {level: index for index, level in enumerate(app.INTENSITY_LEVELS)}
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(
                count=16, prompt_seed=seed, content_mode="progressive"
            )
            shots = state.get_storyboard(storyboard_id)["shots"]
            intensities = [order[shot["scene"]["intensity"]] for shot in shots]
            self.assertEqual(intensities, sorted(intensities))
            for shot in shots:
                recipe = shot["scene"].get("explicit_recipe")
                if recipe:
                    self.assertEqual(shot["scene"]["intensity"], recipe["intensity"])

    def test_location_zones_match_pose_capabilities_and_environment(self):
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(count=12, prompt_seed=seed)
            for shot in state.get_storyboard(storyboard_id)["shots"]:
                scene = shot["scene"]
                app.validate_pose_zone(scene["pose"], scene["location_zone"])
                allowed = set(scene["location_zone"]["environment_tags"])
                self.assertTrue(allowed & app.tags(scene["interior"]))

    def test_subject_has_one_or_two_compatible_facial_accents(self):
        for seed in range(40):
            state, storyboard_id = self.make_storyboard(prompt_seed=seed)
            accents = state.get_storyboard(storyboard_id)["shots"][0]["context"]["human"]["facial_accents"]
            self.assertIn(len(accents), {1, 2})
            self.assertEqual(len({item["id"] for item in accents}), len(accents))

    def test_sequence_seeds_are_stable_and_unique(self):
        first_state, first_id = self.make_storyboard(inference_strategy="sequence")
        second_state, second_id = self.make_storyboard(inference_strategy="sequence")
        first = [shot["inference_seed"] for shot in first_state.storyboard_payload(first_state.get_storyboard(first_id))["shots"]]
        second = [shot["inference_seed"] for shot in second_state.storyboard_payload(second_state.get_storyboard(second_id))["shots"]]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))

    def test_single_frame_job_targets_only_requested_shot(self):
        state, storyboard_id = self.make_storyboard()
        with patch.object(app.threading, "Thread"):
            job = state.create_job(storyboard_id, False, [3])
        self.assertEqual(job["kind"], "shot")
        self.assertEqual(job["shot_numbers"], [3])
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["logs"][0]["type"], "queued")
        self.assertEqual(job["logs"][0]["total"], 1)
        self.assertNotIn("attempt", job)
        self.assertNotIn("retrying", job)
        self.assertNotIn("last_error", job)

    def test_jobs_publish_orthogonal_generation_mode_and_render_tier(self):
        state, storyboard_id = self.make_storyboard(mode="random")
        with patch.object(app.threading, "Thread"):
            random_job = state.create_job(storyboard_id, False, [1, 2])
            preview_job = state.create_job(storyboard_id, True, [1])
        self.assertEqual(random_job["generation_mode"], "random")
        self.assertEqual(random_job["render_tier"], "production")
        self.assertEqual(random_job["pending_groups"][0]["generation_mode"], "random")
        self.assertEqual(random_job["pending_groups"][0]["render_tier"], "production")
        self.assertEqual(preview_job["generation_mode"], "random")
        self.assertEqual(preview_job["render_tier"], "preview")
        self.assertNotIn("render_kind", random_job)

    def test_render_jobs_queue_in_fifo_order_and_snapshot_the_storyboard(self):
        state, storyboard_id = self.make_storyboard()
        original_prompt = state.get_storyboard(storyboard_id)["shots"][0]["scene"]["pose"]["prompt"]
        with patch.object(app.threading, "Thread") as thread:
            first = state.create_job(storyboard_id, False)
            second = state.create_job(storyboard_id, True, [1])
        self.assertEqual(first["queue_position"], 1)
        self.assertEqual(second["queue_position"], 2)
        self.assertEqual(thread.call_count, 1)
        state.get_storyboard(storyboard_id)["shots"][0]["scene"]["pose"]["prompt"] = "changed later"
        self.assertEqual(state.jobs[first["id"]]["_shots"][0]["scene"]["pose"]["prompt"], original_prompt)

        order = []
        def complete(job_id):
            order.append(job_id)
            state.jobs[job_id]["status"] = "completed"

        state._job_worker_running = True
        with patch.object(state, "_run_job", side_effect=complete):
            state._run_job_queue()
        self.assertEqual(order, [first["id"], second["id"]])
        self.assertFalse(state._job_worker_running)

    def test_render_jobs_expose_logical_pending_groups_without_queued_eta(self):
        state, storyboard_id = self.make_storyboard()
        with patch.object(app.threading, "Thread"):
            job = state.create_job(storyboard_id, False, [1, 2, 3])
        group = job["pending_groups"][0]
        self.assertEqual(group["job_id"], job["id"])
        self.assertEqual(group["positions"], [1, 2, 3])
        self.assertEqual(group["shot_numbers"], [1, 2, 3])
        self.assertEqual(group["status"], "queued")
        self.assertIsNone(group["frame_eta_seconds"])
        self.assertIsNone(group["group_eta_seconds"])
        self.assertNotIn("pending_frames", job)

        timing_key = (False, job["workflow_profile"])
        state._render_timings[timing_key] = [8.0, 10.0, 60.0]
        estimated = state.get_job(job["id"])
        self.assertEqual(estimated["estimated_frame_seconds"], 10.0)
        self.assertIsNone(estimated["pending_groups"][0]["frame_eta_seconds"])
        self.assertIsNone(estimated["pending_groups"][0]["group_eta_seconds"])
        self.assertTrue(estimated["pending_groups"][0]["observed_at"])

        record = state.jobs[job["id"]]
        record["total"] = 2000
        record["shot_numbers"] = list(range(1, 2001))
        record["render_groups"] = [{
            "group_index": 1,
            "positions": list(range(1, 2001)),
            "shot_numbers": list(range(1, 2001)),
        }]
        large = state.job_payload(record)
        self.assertEqual(len(large["pending_groups"]), 1)
        self.assertEqual(len(large["pending_groups"][0]["positions"]), 2000)

    def test_queued_jobs_do_not_publish_speculative_eta(self):
        state, storyboard_id = self.make_storyboard()
        with patch.object(app.threading, "Thread"):
            first = state.create_job(storyboard_id, False, [1, 2])
            second = state.create_job(storyboard_id, False, [3])
            preview = state.create_job(storyboard_id, True, [4])
        state._render_timings[(False, first["workflow_profile"])] = [12.0]
        session = state.jobs_payload()
        queued = {job["id"]: job for job in session["queued_jobs"]}
        self.assertIsNone(queued[first["id"]]["pending_groups"][0]["frame_eta_seconds"])
        self.assertIsNone(queued[second["id"]]["pending_groups"][0]["frame_eta_seconds"])
        self.assertIsNone(queued[preview["id"]]["estimated_frame_seconds"])
        self.assertIsNone(queued[preview["id"]]["pending_groups"][0]["frame_eta_seconds"])

        cancelled = state.cancel_job(second["id"])
        self.assertEqual(cancelled["pending_groups"], [])

    def test_running_pending_frame_is_rendering_and_countdown_never_negative(self):
        state, storyboard_id = self.make_storyboard()
        with patch.object(app.threading, "Thread"):
            job = state.create_job(storyboard_id, False, [1, 2, 3])
        record = state.jobs[job["id"]]
        record["status"] = "running"
        record["completed"] = 1
        record["_frame_durations"] = [10.0]
        record["_started_monotonic"] = app.time.monotonic() - 15
        record["_shot_started_monotonic"] = app.time.monotonic() - 20
        payload = state.get_job(job["id"])
        self.assertEqual(payload["pending_groups"][0]["status"], "rendering")
        self.assertEqual(payload["pending_groups"][0]["positions"][0], 2)
        self.assertEqual(payload["pending_groups"][0]["frame_eta_seconds"], 0.0)
        self.assertEqual(payload["pending_groups"][0]["group_eta_seconds"], 10.0)
        self.assertEqual(payload["eta_seconds"], 10.0)

    def test_queued_job_can_be_cancelled_before_it_starts(self):
        state, storyboard_id = self.make_storyboard()
        with patch.object(app.threading, "Thread"):
            first = state.create_job(storyboard_id, False)
            second = state.create_job(storyboard_id, False)
        cancelled = state.cancel_job(second["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["logs"][-1]["message"], "Queued render cancelled")
        session = state.jobs_payload()
        self.assertEqual(session["active_job"]["id"], first["id"])
        self.assertEqual([job["id"] for job in session["queued_jobs"]], [first["id"]])

    def test_new_variation_changes_only_the_shot_inference_seed(self):
        state, storyboard_id = self.make_storyboard()
        before = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"][1]
        after = state.randomize_shot_seed(storyboard_id, 2)
        self.assertNotEqual(before["inference_seed"], after["inference_seed"])
        self.assertTrue(after["seed_manual"])
        self.assertEqual(before["positive_prompt"], after["positive_prompt"])
        self.assertEqual(before["selected_ids"], after["selected_ids"])

    def test_changing_variation_settings_preserves_director_custom_values(self):
        state, storyboard_id = self.make_storyboard()
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "shot.pose", "custom_value": "custom held pose"},
        )
        before = state.get_storyboard(storyboard_id)["shots"][0]
        scene_id = id(before["scene"])
        updated = state.update_storyboard_seeds(
            storyboard_id,
            {"inference_seed": 987654, "inference_strategy": "sequence"},
        )
        after = state.get_storyboard(storyboard_id)["shots"][0]
        self.assertEqual(id(after["scene"]), scene_id)
        self.assertEqual(after["custom_values"]["shot.pose"], "custom held pose")
        self.assertIn("custom held pose", updated["shots"][0]["positive_prompt"])

    def test_custom_subject_value_updates_director_summary(self):
        state, storyboard_id = self.make_storyboard()
        updated = state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.hair_color",
                "custom_value": "vivid copper hair",
            },
        )
        self.assertIn("vivid copper hair", updated["summary"]["subject"])

    def test_set_scoped_custom_age_propagates_across_only_the_current_set(self):
        state, storyboard_id = self.make_storyboard(photoshoots=2, count=3)
        marker = "custom adult age prompt"
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "human.age", "custom_value": marker},
        )

        record = state.get_storyboard(storyboard_id)
        first_set = [
            shot for shot in record["shots"] if shot["photoshoot_index"] == 0
        ]
        second_set = [
            shot for shot in record["shots"] if shot["photoshoot_index"] == 1
        ]
        self.assertTrue(all(
            shot["context"]["custom_values"]["human.age"] == marker
            and shot["scene"]["custom_values"]["human.age"] == marker
            and marker in app.serialize_shot(record["db"], shot)["positive_prompt"]
            for shot in first_set
        ))
        self.assertTrue(all(
            "human.age" not in shot["context"].get("custom_values", {})
            and marker not in app.serialize_shot(record["db"], shot)["positive_prompt"]
            for shot in second_set
        ))
        self.assertEqual(
            director_fields(state.director_payload(storyboard_id, 2))[
                "human.age"
            ]["custom"],
            marker,
        )
        self.assertEqual(
            director_fields(state.director_payload(storyboard_id, 4))[
                "human.age"
            ]["custom"],
            "",
        )

    def test_set_scoped_custom_value_can_propagate_to_all_photoshoots(self):
        state, storyboard_id = self.make_storyboard(photoshoots=2, count=2)
        marker = "custom identity for every set"
        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.age",
                "custom_value": marker,
                "custom_scope": "all",
            },
        )
        record = state.get_storyboard(storyboard_id)
        self.assertTrue(all(
            shot["context"]["custom_values"]["human.age"] == marker
            and marker in app.serialize_shot(record["db"], shot)["positive_prompt"]
            for shot in record["shots"]
        ))

    def test_random_storyboard_custom_value_can_propagate_to_all_shots(self):
        state, storyboard_id = self.make_storyboard(mode="random", count=3)
        marker = "custom identity for every random shot"
        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.hair_color",
                "custom_value": marker,
                "custom_scope": "all",
            },
        )
        record = state.get_storyboard(storyboard_id)
        self.assertTrue(all(
            shot["context"]["custom_values"]["human.hair_color"] == marker
            and marker in app.serialize_shot(record["db"], shot)["positive_prompt"]
            for shot in record["shots"]
        ))

    def test_shot_scoped_custom_value_rejects_all_scope(self):
        state, storyboard_id = self.make_storyboard()
        with self.assertRaisesRegex(app.AppError, "Shot-scoped"):
            state.update_director(
                storyboard_id,
                {
                    "shot": 1,
                    "field": "shot.pose",
                    "custom_value": "custom pose",
                    "custom_scope": "all",
                },
            )

    def test_custom_skin_marking_is_used_when_that_identity_detail_is_visible(self):
        state, storyboard_id = self.make_storyboard(content_mode="xxx")
        record = state.get_storyboard(storyboard_id)
        position = next(
            shot["number"] for shot in record["shots"]
            if set(shot["stage"].get("body_visibility", []))
            & {"nipples", "pubic_area", "genitals"}
        )
        marker = "custom sun-kissed skin markings"
        state.update_director(
            storyboard_id,
            {
                "shot": position,
                "field": "human.skin_marking",
                "custom_value": marker,
            },
        )
        updated = state.storyboard_payload(state.get_storyboard(storyboard_id))
        self.assertIn(marker, updated["shots"][position - 1]["positive_prompt"])

    def test_set_scoped_subject_change_does_not_cross_photoshoots(self):
        state, storyboard_id = self.make_storyboard(photoshoots=2, count=3)
        record = state.get_storyboard(storyboard_id)
        second_set_before = [
            shot["scene"]["human"]["hair_color"]["id"]
            for shot in record["shots"] if shot["photoshoot_index"] == 1
        ]
        field = director_fields(state.director_payload(storyboard_id, 1))["human.hair_color"]
        replacement = next(
            option["id"] for option in field["options"]
            if option["id"] and option["id"] != field["value"]
        )
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "human.hair_color", "value": replacement},
        )
        shots = state.get_storyboard(storyboard_id)["shots"]
        self.assertTrue(all(
            shot["scene"]["human"]["hair_color"]["id"] == replacement
            for shot in shots if shot["photoshoot_index"] == 0
        ))
        self.assertEqual(second_set_before, [
            shot["scene"]["human"]["hair_color"]["id"]
            for shot in shots if shot["photoshoot_index"] == 1
        ])

    def test_stage_change_is_manual_and_rebuilds_compatible_direction(self):
        state, storyboard_id = self.make_storyboard()
        fields = director_fields(state.director_payload(storyboard_id, 1))
        covered = next(
            option for option in fields["shot.stage"]["options"]
            if option["prompt"] == "covered"
        )
        updated = state.update_director(
            storyboard_id,
            {"shot": 1, "field": "shot.stage", "value": covered["id"]},
        )
        updated_fields = director_fields(updated)
        self.assertTrue(updated["summary"]["stage"]["manual"])
        self.assertGreater(updated_fields["shot.stage"]["compatibility"]["poses"], 0)
        self.assertGreater(updated_fields["shot.stage"]["compatibility"]["actions"], 0)

    def test_custom_to_preset_is_one_atomic_director_update(self):
        state, storyboard_id = self.make_storyboard()
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "human.hair_color", "custom_value": "custom hair"},
        )
        fields = director_fields(state.director_payload(storyboard_id, 1))
        preset = fields["human.hair_color"]["options"][0]["id"]
        updated = state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.hair_color",
                "value": preset,
                "clear_custom": True,
            },
        )
        self.assertEqual(director_fields(updated)["human.hair_color"]["custom"], "")

    def test_random_director_value_uses_a_compatible_nonempty_option(self):
        state, storyboard_id = self.make_storyboard()
        before = director_fields(state.director_payload(storyboard_id, 1))
        field = before["shot.pose"]
        compatible = {option["id"] for option in field["options"] if option["id"]}
        updated = state.update_director(
            storyboard_id,
            {"shot": 1, "field": "shot.pose", "value": "__director_random__"},
        )
        selected = director_fields(updated)["shot.pose"]["value"]
        self.assertIn(selected, compatible)
        if len(compatible) > 1:
            self.assertNotEqual(selected, field["value"])

    def test_custom_values_survive_export_import_and_reroll(self):
        state, storyboard_id = self.make_storyboard()
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "human.hair_color", "custom_value": "exported hair"},
        )
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "shot.pose", "custom_value": "exported pose"},
        )
        imported = state.import_storyboard(state.export_storyboard(storyboard_id))
        imported_id = imported["id"]
        state.reroll_shot(imported_id, 1)
        shot = state.storyboard_payload(state.get_storyboard(imported_id))["shots"][0]
        self.assertIn("exported hair", shot["positive_prompt"])
        self.assertIn("exported pose", shot["positive_prompt"])

    def test_photoshoot_stages_never_move_back_toward_more_clothing(self):
        levels = {"covered": 0, "lingerie": 1, "topless": 2, "nude": 3, "explicit": 4}
        for seed in range(40):
            state, storyboard_id = self.make_storyboard(
                count=12, prompt_seed=seed,
                content_mode="xxx" if seed % 2 else "progressive",
            )
            shots = state.get_storyboard(storyboard_id)["shots"]
            progression = [levels[shot["scene"]["stage"]["level"]] for shot in shots]
            self.assertEqual(progression, sorted(progression), (seed, progression))

    def test_zero_nsfw_and_plateau_never_resolve_nude_stages(self):
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(
                count=20,
                prompt_seed=seed,
                nsfw_percent=0,
                plateau_percent=0,
            )
            levels = {
                shot["scene"]["stage"]["level"]
                for shot in state.get_storyboard(storyboard_id)["shots"]
            }
            self.assertTrue(levels.issubset({"covered", "lingerie"}), (seed, levels))

    def test_compiler_preserves_stage_and_visible_garments(self):
        anchors = {
            "covered": "fully opaque upper-body garment",
            "lingerie": "one fully opaque chest-covering lingerie garment",
            "topless": "topless, bare breasts and visible nipples",
            "nude": "fully nude body",
            "explicit": "explicit adult pose",
        }
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=8800)
        record = state.get_storyboard(storyboard_id)
        payload = state.storyboard_payload(record)
        for shot, rendered in zip(record["shots"], payload["shots"]):
            scene = shot["scene"]
            positive = rendered["positive_prompt"].casefold()
            expected_anchor = (
                "anatomically coherent rear-facing nude pose"
                if scene["stage"].get("plateau_kind") == "provocative_rear"
                else "fully opaque fitted lining beneath the translucent outer garment"
                if scene["stage"]["level"] == "covered" and any(
                    "sheer" in app.tags(scene["outfit"]["garments"][slot])
                    for slot in scene["stage"].get("visible_slots", [])
                    if slot in scene["outfit"]["garments"]
                )
                else anchors[scene["stage"]["level"]]
            )
            self.assertIn(expected_anchor, positive)
            self.assertEqual(
                len(rendered["selected_ids"]), len(set(rendered["selected_ids"]))
            )
            for slot in scene["stage"].get("visible_slots", []):
                garment = scene["outfit"]["garments"].get(slot)
                if garment:
                    self.assertIn(garment["prompt"].casefold(), positive)

    def test_rear_recipe_enforces_orientation_without_competing_hand_action(self):
        state, storyboard_id = self.make_storyboard(count=4, prompt_seed=97531)
        record = state.get_storyboard(storyboard_id)
        shot = record["shots"][-1]
        db = record["db"]
        recipe = next(
            item for item in db["explicit_recipes"] if item["id"] == "recipe_bent_over"
        )
        shot["stage"]["level"] = "explicit"
        shot["stage"].pop("plateau_kind", None)
        shot["stage"]["visible_slots"] = []
        shot["stage"]["body_visibility"] = ["breasts", "nipples", "pubic_area", "genitals"]
        shot["scene"]["stage"] = shot["stage"]
        shot["scene"]["explicit_recipe"] = recipe
        shot["scene"]["garment_transition"] = {
            "id": "transition_test",
            "prompt": "deliberately removing bra and panties with both hands",
            "slots": ["bra", "panties"],
        }

        positive, negative, _ = app.compile_scene(db, shot["scene"])
        folded = positive.casefold()
        self.assertIn("anatomically coherent rear-facing nude pose", folded)
        self.assertNotIn("bare breasts, visible nipples", folded)
        self.assertNotIn("deliberately removing", folded)
        self.assertNotIn("orgasmic facial expression", folded)
        self.assertIn("face turned mostly away from the camera", folded)
        self.assertIn("front-facing torso", negative.casefold())
        self.assertLess(
            folded.index("rear-facing nude pose"),
            folded.index(shot["scene"]["human"]["ethnic_appearance"]["prompt"].casefold()),
        )

    def test_explicit_recipe_focus_constrains_pose_action_and_director_options(self):
        checked = 0
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(
                count=8, prompt_seed=seed, content_mode="xxx"
            )
            record = state.get_storyboard(storyboard_id)
            for position, shot in enumerate(record["shots"], 1):
                scene = shot["scene"]
                recipe = scene.get("explicit_recipe")
                if recipe is None:
                    continue
                checked += 1
                self.assertTrue(app.recipe_focus_compatible(scene["pose"], recipe, "pose"))
                self.assertTrue(app.recipe_focus_compatible(scene["action"], recipe, "action"))
                fields = director_fields(state.director_payload(storyboard_id, position))
                for option in fields["shot.pose"]["options"]:
                    item = next(item for item in record["db"]["poses"] if item["id"] == option["id"])
                    self.assertTrue(app.recipe_focus_compatible(item, recipe, "pose"))
                for option in fields["shot.action"]["options"]:
                    item = next(item for item in record["db"]["actions"] if item["id"] == option["id"])
                    self.assertTrue(app.recipe_focus_compatible(item, recipe, "action"))
        self.assertGreater(checked, 40)

    def test_rear_kneeling_and_intimate_display_records_are_reachable(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(0))
        context = composer.fixed_context()
        explicit = next(
            stage for stage in app.effective_photoshoot_stages(
                context["outfit"]["template"]
            )
            if stage["level"] == "explicit"
        )
        for pose_id in ("pose_explicit_rear_kneeling", "pose_curated_09"):
            scene = composer.resolve_scene(context, explicit, {
                "explicit_recipe": "recipe_rear_kneeling", "pose": pose_id,
            })
            self.assertEqual(scene["pose"]["id"], pose_id)
            self.assertEqual(scene["explicit_recipe"]["id"], "recipe_rear_kneeling")
        scene = composer.resolve_scene(context, explicit, {
            "explicit_recipe": "recipe_intimate_macro",
            "action": "action_intimate_closeup_display",
        })
        self.assertEqual(
            scene["action"]["id"], "action_intimate_closeup_display"
        )

    def test_automatic_garment_transition_describes_state_not_second_action(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=24680)
        transitions = [
            shot["scene"]["garment_transition"]["prompt"]
            for shot in state.get_storyboard(storyboard_id)["shots"]
            if shot["scene"].get("garment_transition")
        ]
        self.assertTrue(transitions)
        for prompt in transitions:
            self.assertTrue(prompt.startswith("fully removed and no longer wearing "))
            self.assertNotIn("deliberately removing", prompt)

        shots = state.get_storyboard(storyboard_id)["shots"]
        removed = set()
        for shot in shots:
            visible = set(shot["stage"].get("visible_slots", []))
            self.assertFalse(visible & removed)
            removed = set(shot["scene"]["removed_garment_slots"])

    def test_compiler_uses_declared_diffusion_priority(self):
        state, storyboard_id = self.make_storyboard(count=8, prompt_seed=4141)
        record = state.get_storyboard(storyboard_id)
        shot = record["shots"][0]
        scene = shot["scene"]
        positive = state.storyboard_payload(record)["shots"][0]["positive_prompt"].casefold()
        first_visible_garment = next(
            scene["outfit"]["garments"][slot]
            for slot in scene["outfit"]["template"]["slots"]
            if slot in set(scene["stage"].get("visible_slots", []))
            and slot in scene["outfit"]["garments"]
        )
        positions = [
            positive.index("single subject"),
            positive.index(scene["pose"]["prompt"].casefold()),
            positive.index(scene["shot_size"]["prompt"].casefold()),
            positive.index(scene["human"]["ethnic_appearance"]["prompt"].casefold()),
            positive.index(first_visible_garment["prompt"].casefold()),
            positive.index(scene["interior"]["prompt"].casefold()),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            record["db"]["prompt_defaults"]["prompt_priority"],
            [
                "subject", "camera_direction", "anatomy",
                "traits_garments", "location_treatment",
            ],
        )

    def test_photoshoot_prompts_repeat_stable_age_anatomy_and_visible_colors_early(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=616161)
        record = state.get_storyboard(storyboard_id)
        payload = state.storyboard_payload(record)
        human = record["shots"][0]["scene"]["human"]
        for source, rendered in zip(record["shots"], payload["shots"]):
            positive = rendered["positive_prompt"].casefold()
            visible_anatomy = bool(
                {"breasts", "nipples"} & set(source["stage"].get("body_visibility", []))
            )
            breast_size = human["breast_size"][
                "prompt" if visible_anatomy else "covered_prompt"
            ]
            breast_shape = human["breast_shape"][
                "prompt" if visible_anatomy else "covered_prompt"
            ]
            hair = " ".join(
                human[key]["prompt"] for key in (
                    "hair_texture", "hair_length", "hair_style", "hair_color",
                )
            )
            stable_subject = (
                f"subject has {breast_size} with {breast_shape}; subject has {hair}"
            ).casefold()
            self.assertIn(stable_subject, positive)
            self.assertEqual(positive.count(stable_subject), 1)
            self.assertEqual(positive.count(human["age"]["prompt"].casefold()), 1)
            self.assertNotIn("consistent face and body in every frame", positive)
            self.assertLess(positive.index(stable_subject), positive.index(source["scene"]["pose"]["prompt"].casefold()))
            visible = set(source["stage"].get("visible_slots", []))
            for slot in visible:
                if slot not in source["scene"]["outfit"]["garments"]:
                    continue
                color = source["scene"]["outfit"]["colors"][slot]["prompt"].casefold()
                self.assertIn("fixed wardrobe colors for this frame:", positive)
                self.assertLess(positive.index(color), positive.index(source["scene"]["pose"]["prompt"].casefold()))

    def test_partly_undressed_stage_does_not_put_a_bra_over_opaque_upperwear(self):
        database, _ = app.load_database()
        template = next(
            item for item in database["outfit_templates"]
            if item["id"] == "template_progressive_undressing"
        )
        opaque_upperwear = next(
            item for item in database["garments"]["upperwear"]
            if item["id"] == "top_corset"
        )
        configured = copy.deepcopy(database)
        for item in configured["garments"]["upperwear"]:
            item["disabled"] = item["id"] != opaque_upperwear["id"]
        composer = app.Composer(configured, app.random.Random(77881))
        outfit = composer.choose_outfit(template)
        context = composer.fixed_context()
        context["outfit"] = outfit
        stage = next(item for item in template["stages"] if item["id"] == "undress_lower_removed")
        scene = composer.resolve_scene(context, stage)
        positive, _, _ = app.compile_scene(configured, scene)
        folded = positive.casefold()
        self.assertIn("one fully opaque upper-body garment as the visible chest layer", folded)
        self.assertNotIn("one fully opaque chest-covering lingerie garment", folded)
        self.assertNotIn("underlying", folded)

    def test_sheer_layers_and_hosiery_coordinate_underwear_color_and_pattern(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(404404))
        relations = database["settings"]["wardrobe_compatibility"]["visible_layer_rules"]
        checked = {rule["id"]: 0 for rule in relations}
        for _ in range(1200):
            template = composer.choose_template()
            outfit = composer.choose_outfit(template)
            garments = outfit["garments"]
            colors = outfit["colors"]
            patterns = outfit["patterns"]
            for relation in relations:
                inner_slot = relation["inner_slot"]
                outer_slot = next((
                    slot for slot in relation["outer_slots"]
                    if slot in garments
                    and app.tags(garments[slot]) & set(relation["outer_tags_any"])
                ), None)
                if outer_slot is None or inner_slot not in garments:
                    continue
                checked[relation["id"]] += 1
                self.assertEqual(
                    app.color_family(database, colors[outer_slot]["id"]),
                    app.color_family(database, colors[inner_slot]["id"]),
                )

                if relation["suppress_inner_pattern"]:
                    self.assertNotIn(inner_slot, patterns)
        self.assertTrue(all(count > 10 for count in checked.values()), checked)

    def test_director_changes_a_coordinated_lingerie_color_as_one_group(self):
        state, storyboard_id = self.make_storyboard(count=4, prompt_seed=8801)
        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "outfit.template",
                "value": "template_revealing_boudoir",
            },
        )
        fields = director_fields(state.director_payload(storyboard_id, 1))
        bra = fields["outfit.colors.bra"]
        panties = fields["outfit.colors.panties"]
        self.assertEqual(
            {option["id"] for option in bra["options"]},
            {option["id"] for option in panties["options"]},
        )
        replacement = next(
            option["id"] for option in bra["options"]
            if option["id"] != bra["value"]
        )
        state.update_director(
            storyboard_id,
            {"shot": 1, "field": "outfit.colors.bra", "value": replacement},
        )
        for shot in state.get_storyboard(storyboard_id)["shots"]:
            colors = shot["context"]["outfit"]["colors"]
            self.assertEqual(colors["bra"]["id"], replacement)
            self.assertEqual(colors["panties"]["id"], replacement)

    def test_optional_bra_relation_is_data_driven_and_drops_invalid_stages(self):
        database, _ = app.load_database()
        configured = copy.deepcopy(database)
        relation = next(
            item for item in configured["settings"]["wardrobe_compatibility"]["optional_inner_layers"]
            if item["id"] == "braless_self_supporting_upperwear"
        )
        relation["chance"] = 1
        template = next(
            item for item in configured["outfit_templates"]
            if item["id"] == "template_everyday_casual"
        )
        for item in configured["garments"]["upperwear"]:
            item["disabled"] = item["id"] != "top_fitted_oxford_shirt"
        composer = app.Composer(configured, app.random.Random(818181))
        outfit = composer.choose_outfit(template)
        self.assertNotIn("bra", outfit["garments"])
        self.assertTrue(all(
            "bra" not in stage.get("visible_slots", [])
            for stage in outfit["template"]["stages"]
        ))
        self.assertTrue(all(
            not (
                stage["level"] == "lingerie"
                and not set(stage.get("visible_slots", []))
                & {"upperwear", "full_body", "outerwear"}
            )
            for stage in outfit["template"]["stages"]
        ))
        app.validate_outfit_layers(configured, outfit)

        incompatible = copy.deepcopy(outfit)
        incompatible["garments"]["upperwear"] = next(
            item for item in configured["garments"]["upperwear"]
            if item["id"] == "top_cotton_tee"
        )
        with self.assertRaisesRegex(app.AppError, "Required garment slot bra"):
            app.validate_outfit_layers(configured, incompatible)

    def test_optional_bra_probability_zero_preserves_required_bra(self):
        database, _ = app.load_database()
        configured = copy.deepcopy(database)
        relation = next(
            item for item in configured["settings"]["wardrobe_compatibility"]["optional_inner_layers"]
            if item["id"] == "braless_self_supporting_upperwear"
        )
        relation["chance"] = 0
        template = next(
            item for item in configured["outfit_templates"]
            if item["id"] == "template_everyday_casual"
        )
        for item in configured["garments"]["upperwear"]:
            item["disabled"] = item["id"] != "top_fitted_oxford_shirt"
        outfit = app.Composer(configured, app.random.Random(919191)).choose_outfit(template)
        self.assertIn("bra", outfit["garments"])
        self.assertTrue(any(
            "bra" in stage.get("visible_slots", [])
            for stage in outfit["template"]["stages"]
        ))

    def test_exposed_outer_construction_forces_a_natural_braless_progression(self):
        database, _ = app.load_database()
        relation = next(
            item for item in database["settings"]["wardrobe_compatibility"]["optional_inner_layers"]
            if item["id"] == "braless_required_by_outer_construction"
        )
        self.assertEqual(relation["chance"], 1)
        template_id = "template_satin_separates"

        def resolve_with_only(upperwear_id):
            configured = copy.deepcopy(database)
            template = next(
                item for item in configured["outfit_templates"]
                if item["id"] == template_id
            )
            for item in configured["garments"]["upperwear"]:
                item["disabled"] = item["id"] != upperwear_id
            return app.Composer(
                configured, app.random.Random(515151)
            ).choose_outfit(template)

        camisole = resolve_with_only("top_satin_camisole")
        self.assertNotIn("bra", camisole["garments"])
        self.assertTrue(all(
            "bra" not in stage.get("visible_slots", [])
            for stage in camisole["template"]["stages"]
        ))
        self.assertFalse(any(
            stage["level"] == "lingerie"
            for stage in camisole["template"]["stages"]
        ))

        blouse = resolve_with_only("top_silk_blouse")
        self.assertIn("bra", blouse["garments"])
        self.assertTrue(any(
            "bra" in stage.get("visible_slots", [])
            for stage in blouse["template"]["stages"]
        ))

        for template_id, full_body_id in (
            ("template_day_dress", "swimsuit_classic"),
            ("template_cocktail_dress", "dress_satin_cowl"),
        ):
            configured = copy.deepcopy(database)
            template = next(
                item for item in configured["outfit_templates"]
                if item["id"] == template_id
            )
            for item in configured["garments"]["full_body"]:
                item["disabled"] = item["id"] != full_body_id
            outfit = app.Composer(
                configured, app.random.Random(616161)
            ).choose_outfit(template)
            self.assertNotIn("bra", outfit["garments"])
            self.assertTrue(all(
                "bra" not in stage.get("visible_slots", [])
                for stage in outfit["template"]["stages"]
            ))

    def test_braless_shaping_dress_remains_structurally_sfw(self):
        database, _ = app.load_database()
        configured = copy.deepcopy(database)
        relation = next(
            item for item in configured["settings"]["wardrobe_compatibility"]["optional_inner_layers"]
            if item["id"] == "braless_self_supporting_upperwear"
        )
        relation["chance"] = 1
        template = next(
            item for item in configured["outfit_templates"]
            if item["id"] == "template_day_dress"
        )
        for item in configured["garments"]["full_body"]:
            item["disabled"] = item["id"] != "dress_scoopneck_shaping"
        composer = app.Composer(configured, app.random.Random(717171))
        outfit = composer.choose_outfit(template, content_mode="sfw")
        self.assertNotIn("bra", outfit["garments"])
        app.validate_sfw_outfit(outfit)
        covered = next(stage for stage in outfit["template"]["stages"] if stage["level"] == "covered")
        context = composer.fixed_context("sfw")
        context["outfit"] = outfit
        positive, _, selected = app.compile_scene(
            configured, composer.resolve_scene(context, covered)
        )
        self.assertIn("body-shaping scoop-neck jersey dress", positive)
        self.assertFalse(any(item_id.startswith("bra_") for item_id in selected))

    def test_color_compatibility_relations_live_in_database(self):
        database, _ = app.load_database()
        settings = database["settings"]["wardrobe_compatibility"]
        configured_colors = [
            color_id
            for members in settings["color_families"].values()
            for color_id in members
        ]
        self.assertEqual(set(configured_colors), {item["id"] for item in database["colors"]})
        self.assertEqual(len(configured_colors), len(set(configured_colors)))
        self.assertEqual(
            {rule["id"] for rule in settings["visible_layer_rules"]},
            {"sheer_chest_over_bra", "sheer_lower_over_panties", "hosiery_over_panties"},
        )
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("COLOR_TONE_FAMILIES", source)

    def test_revealing_templates_exclude_self_contained_bodysuits_from_bra_layering(self):
        database, _ = app.load_database()
        blocked = {"bodysuit_lace", "bodysuit_sheer", "bodysuit_strappy", "bodysuit_leather", "swimsuit_micro"}
        for template_id in ("template_revealing_boudoir", "template_pearl_boudoir"):
            template = next(item for item in database["outfit_templates"] if item["id"] == template_id)
            reachable = {
                item["id"] for item in database["garments"]["full_body"]
                if app.garment_matches_template_slot(database, template, "full_body", item)
            }
            self.assertFalse(blocked & reachable, (template_id, blocked & reachable))

    def test_default_lingerie_distribution_does_not_overselect_satin_or_pearls(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(20260724))
        total = satin = pearl = 0
        colors: Counter[str] = Counter()
        for _ in range(1500):
            outfit = composer.choose_outfit(composer.choose_template())
            for slot in ("bra", "panties"):
                garment = outfit["garments"].get(slot)
                if not garment:
                    continue
                total += 1
                wording = f"{garment['id']} {garment['prompt']}".casefold()
                satin += int("satin" in wording or "silk" in app.tags(garment))
                pearl += int("pearl" in wording)
                colors[outfit["colors"][slot]["id"]] += 1
        self.assertGreater(total, 2000)
        self.assertLess(satin / total, 0.2)
        self.assertLess(pearl / total, 0.15)
        self.assertLess(colors["color_black"] / total, 0.45)
        self.assertGreaterEqual(len(colors), 15)

    def test_opaque_boudoir_outer_layer_hides_bra_and_chest_visibility(self):
        database, _ = app.load_database()
        configured = copy.deepcopy(database)
        template = next(
            item for item in configured["outfit_templates"]
            if item["id"] == "template_pearl_boudoir"
        )
        for item in configured["garments"]["full_body"]:
            item["disabled"] = item["id"] != "nightgown_satin"
        composer = app.Composer(configured, app.random.Random(515151))
        outfit = composer.choose_outfit(template)
        layered = next(stage for stage in outfit["template"]["stages"] if stage["id"] == "pearl_boudoir_layered")
        self.assertNotIn("bra", layered["visible_slots"])
        self.assertFalse({"breasts", "nipples"} & set(layered["body_visibility"]))
        context = composer.fixed_context()
        context["outfit"] = outfit
        positive, _, selected = app.compile_scene(
            configured, composer.resolve_scene(context, layered)
        )
        self.assertIn("long satin nightgown with a high slit", positive)
        self.assertNotIn(outfit["garments"]["bra"]["prompt"], positive)
        self.assertNotIn(outfit["garments"]["bra"]["id"], selected)
        for anatomy in ("breast", "nipple", "areola"):
            self.assertNotIn(anatomy, positive.casefold())

    def test_visible_sheer_bra_uses_continuous_fabric_render_contract(self):
        database, _ = app.load_database()
        configured = copy.deepcopy(database)
        template = next(
            item for item in configured["outfit_templates"]
            if item["id"] == "template_revealing_boudoir"
        )
        for item in configured["garments"]["full_body"]:
            item["disabled"] = item["id"] != "dress_mesh_mini"
        for item in configured["garments"]["bra"]:
            item["disabled"] = item["id"] != "bra_sheer"
        composer = app.Composer(configured, app.random.Random(616161))
        outfit = composer.choose_outfit(template)
        stage = next(item for item in outfit["template"]["stages"] if item["id"] == "revealing_boudoir_layered")
        context = composer.fixed_context()
        context["outfit"] = outfit
        positive, _, _ = app.compile_scene(
            configured, composer.resolve_scene(context, stage)
        )
        contract = configured["settings"]["wardrobe_compatibility"]["render_contracts"][0]
        self.assertIn(contract["positive_prompt"], positive)
        self.assertIn("distinct bra underneath", positive)
        self.assertNotIn("fully opaque bra underneath", positive)

    def test_covered_chest_positive_contract_is_anatomy_neutral(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=24680)
        record = state.get_storyboard(storyboard_id)
        payload = state.storyboard_payload(record)
        covered = [
            (raw["scene"], rendered)
            for raw, rendered in zip(record["shots"], payload["shots"])
            if raw["scene"]["stage"]["level"] in {"covered", "lingerie"}
        ]
        self.assertTrue(covered)
        for scene, rendered in covered:
            positive = rendered["positive_prompt"].casefold()
            negative = rendered["negative_prompt"].casefold()
            self.assertIn("fully opaque", positive)
            self.assertIn("uninterrupted", positive)
            self.assertIn("clean smooth garment surface", positive)
            for unsafe_concept in ("breast", "nipple", "areola", "bust"):
                self.assertNotIn(unsafe_concept, positive)
            self.assertIn("nipples clipping through clothing", negative)
            self.assertIn("skin-colored nipples through clothing", negative)
            self.assertIn("areola color visible through fabric", negative)
            self.assertIn("colored nipple shapes on fabric", negative)
            self.assertNotIn("nipple outline through clothes", negative)
            self.assertNotIn("nipples protruding through fabric", negative)

    def test_lingerie_bra_is_compiled_as_one_coherent_layer(self):
        lingerie = None
        for seed in range(20):
            state, storyboard_id = self.make_storyboard(count=12, prompt_seed=seed)
            rendered = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"]
            lingerie = next((
                shot for shot in rendered
                if shot["stage"]["level"] == "lingerie"
                and "multiple bras" in shot["negative_prompt"].casefold()
            ), None)
            if lingerie:
                break
        self.assertIsNotNone(lingerie)
        positive = lingerie["positive_prompt"].casefold()
        negative = lingerie["negative_prompt"].casefold()
        self.assertIn("one single-layer", positive)
        self.assertIn("straps and underband matching the same base color and material", positive)
        self.assertIn("multiple bras", negative)
        self.assertIn("extra bra straps", negative)

    def test_rear_focus_requires_pose_or_action_rear_orientation(self):
        database, _ = app.load_database()
        focus = next(item for item in database["focus_targets"] if item["id"] == "focus_rear")
        self.assertEqual(focus["requires_any_tags"], ["provocative_rear"])

    def test_covered_stage_visibility_gates_breast_traits_from_positive_prompt(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=24680)
        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "human.breast_size",
                "value": "breasts_extremely_large",
            },
        )
        shots = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"]
        covered = next(
            shot for shot in shots if shot["stage"]["level"] in {"covered", "lingerie"}
        )
        nude = next(
            shot for shot in shots if shot["stage"]["level"] in {"nude", "explicit"}
        )
        folded = covered["positive_prompt"].casefold()
        for unsafe_concept in ("breast", "nipple", "areola", "bust"):
            self.assertNotIn(unsafe_concept, folded)
        self.assertIn("fully opaque", folded)
        self.assertIn("extremely large heavy natural breasts", nude["positive_prompt"])
        self.assertNotIn("under opaque clothing", nude["positive_prompt"])

    def test_dress_templates_only_offer_physically_compatible_bra_layers(self):
        state, storyboard_id = self.make_storyboard(count=8, prompt_seed=7788)
        state.update_director(
            storyboard_id,
            {
                "shot": 1,
                "field": "outfit.template",
                "value": "template_sexy_casual_dress",
            },
        )
        fields = director_fields(state.director_payload(storyboard_id, 1))
        bra_ids = {option["id"] for option in fields["outfit.garments.bra"]["options"]}
        dress_ids = {
            option["id"] for option in fields["outfit.garments.full_body"]["options"]
        }
        self.assertIn("bra_tshirt", bra_ids)
        self.assertNotIn("bra_sports", bra_ids)
        self.assertNotIn("bra_longline", bra_ids)
        self.assertIn("dress_tshirt_bodycon", dress_ids)
        self.assertNotIn("dress_offshoulder_knit_mini", dress_ids)
        with self.assertRaises(app.AppError):
            state.update_director(
                storyboard_id,
                {
                    "shot": 1,
                    "field": "outfit.garments.bra",
                    "value": "bra_sports",
                },
            )

    def test_resolved_outfits_always_pass_layer_validation(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(9911))
        protected = {
            template_id
            for rule in database["settings"]["garment_layer_rules"]
            for template_id in rule["template_ids"]
        }
        for template in database["outfit_templates"]:
            if template["id"] not in protected:
                continue
            for _ in range(50):
                app.validate_outfit_layers(
                    database,
                    composer.choose_outfit(template),
                )

    def test_prop_is_directly_editable_with_only_compatible_options(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=13579)
        payload = state.director_payload(storyboard_id, 1)
        field = director_fields(payload)["shot.prop"]
        compatible = [option for option in field["options"] if option["id"]]
        self.assertTrue(compatible)
        selected = compatible[0]
        updated = state.update_director(
            storyboard_id,
            {"shot": 1, "field": "shot.prop", "value": selected["id"]},
        )
        self.assertEqual(director_fields(updated)["shot.prop"]["value"], selected["id"])
        rendered = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"][0]
        self.assertIn(selected["prompt"], rendered["positive_prompt"])

    def test_garment_transition_can_be_changed_disabled_and_customized(self):
        state, storyboard_id = self.make_storyboard(count=12, prompt_seed=24680)
        record = state.get_storyboard(storyboard_id)
        position = next(
            index for index, shot in enumerate(record["shots"], 1)
            if shot["scene"].get("garment_transition")
        )
        field = director_fields(state.director_payload(storyboard_id, position))[
            "shot.garment_transition"
        ]
        alternative = next(
            option for option in field["options"]
            if option["id"] and option["id"] != field["value"]
        )
        state.update_director(
            storyboard_id,
            {
                "shot": position,
                "field": "shot.garment_transition",
                "value": alternative["id"],
            },
        )
        rendered = state.storyboard_payload(state.get_storyboard(storyboard_id))["shots"][position - 1]
        self.assertIn(alternative["prompt"], rendered["positive_prompt"])
        disabled = state.update_director(
            storyboard_id,
            {"shot": position, "field": "shot.garment_transition", "value": ""},
        )
        self.assertIn("shot.garment_transition", director_fields(disabled))
        self.assertNotIn(alternative["prompt"], state.storyboard_payload(
            state.get_storyboard(storyboard_id)
        )["shots"][position - 1]["positive_prompt"])
        state.update_director(
            storyboard_id,
            {
                "shot": position,
                "field": "shot.garment_transition",
                "custom_value": "carefully sliding the dress down",
            },
        )
        self.assertIn("carefully sliding the dress down", state.storyboard_payload(
            state.get_storyboard(storyboard_id)
        )["shots"][position - 1]["positive_prompt"])

    def test_legacy_prompt_profile_is_ignored_without_truncation(self):
        state = app.WebState()
        common = {
            "mode": "photoshoot", "count": 4, "photoshoots": 1,
            "prompt_seed": 31415, "inference_seed": 92653,
        }
        normal = state.create_storyboard(common)
        legacy = state.create_storyboard({**common, "prompt_profile": "compact"})
        normal_prompts = [shot["positive_prompt"] for shot in normal["shots"]]
        legacy_prompts = [shot["positive_prompt"] for shot in legacy["shots"]]
        self.assertEqual(normal_prompts, legacy_prompts)


class PromptDebugLogTests(unittest.TestCase):
    def setUp(self):
        self.profile_registry = patch.object(
            app, "load_workflow_profile_registry",
            return_value={"production": "test-model", "preview": "test-model"},
        )
        self.profile_registry.start()
        self.addCleanup(self.profile_registry.stop)

    def test_age_free_debug_prompt_preserves_the_scene_without_age(self):
        state = app.WebState()
        board = state.create_storyboard({
            "count": 1, "prompt_seed": 313, "inference_seed": 414,
        })
        scene = state.get_storyboard(board["id"])["shots"][0]["scene"]
        rendered, negative, selected = app.compile_scene(
            state.get_storyboard(board["id"])["db"], scene
        )
        debug, debug_negative, debug_selected = app.compile_scene(
            state.get_storyboard(board["id"])["db"], scene, include_age=False
        )
        self.assertIn(scene["human"]["age"]["prompt"], rendered)
        self.assertNotIn(scene["human"]["age"]["prompt"], debug)
        self.assertNotRegex(debug, r"\b(?:21|22|23)-year-old\b")
        self.assertIn("adult woman", debug)
        self.assertEqual(debug_negative, negative)
        self.assertEqual(debug_selected, selected)

    def test_prompt_debug_writer_is_disabled_by_default_and_concurrency_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = app.load_config()
            path = root / "config.json"
            log_path = root / "nested" / "prompts.jsonl"
            config["storage"]["prompt_debug_log"] = {
                "enabled": False, "path": str(log_path),
            }
            path.write_text(app.json.dumps(config), encoding="utf-8")
            with patch.object(app, "config_path", return_value=path):
                app.append_prompt_debug_record({"result": "disabled.png"})
                self.assertFalse(log_path.exists())
                config["storage"]["prompt_debug_log"]["enabled"] = True
                path.write_text(app.json.dumps(config), encoding="utf-8")
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(
                        lambda index: app.append_prompt_debug_record({
                            "result": f"image-{index}.png", "positive": "prompt",
                        }),
                        range(40),
                    ))
            records = [app.json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 40)
            self.assertEqual(
                {record["result"] for record in records},
                {f"image-{index}.png" for index in range(40)},
            )

    def test_production_result_emits_filename_and_age_free_prompt(self):
        state = app.WebState()
        board = state.create_storyboard({
            "count": 1, "prompt_seed": 515, "inference_seed": 616,
        })
        with patch.object(app.threading, "Thread"):
            job = state.create_job(board["id"], False)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rendered.png"
            output.write_bytes(b"image")
            with (
                patch.object(app, "load_workflow_runtime", return_value=({}, {})),
                patch.object(app, "generate_one", return_value=("prompt-id", [output])),
                patch.object(app, "output_payload", return_value={"url": "/rendered.png"}),
                patch.object(app, "append_prompt_debug_record") as append,
            ):
                state._run_job(job["id"])
        self.assertEqual(state.jobs[job["id"]]["status"], "completed")
        published = state.jobs[job["id"]]["outputs"][0]
        self.assertEqual(published["generation_mode"], "photoshoot")
        self.assertEqual(published["render_tier"], "production")
        self.assertEqual(published["group_index"], 1)
        record = append.call_args.args[0]
        self.assertEqual(record["kind"], "render")
        self.assertEqual(record["generation_mode"], "photoshoot")
        self.assertEqual(record["render_tier"], "production")
        self.assertEqual(record["group_index"], 1)
        self.assertEqual(record["result"], "rendered.png")
        self.assertNotRegex(record["positive"], r"\b(?:21|22|23)-year-old\b")
        self.assertIn("adult woman", record["positive"])
        self.assertEqual(record["lora_rules"], [])


class PreviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.profile_registry = patch.object(
            app, "load_workflow_profile_registry",
            return_value={"production": "test-model", "preview": "test-model"},
        )
        self.profile_registry.start()
        self.addCleanup(self.profile_registry.stop)

    def test_preview_lifecycle_keeps_image_in_memory(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 1, "prompt_seed": 11, "inference_seed": 22}
        )
        with (
            patch.object(app, "load_workflow_runtime", return_value=({}, {})),
            patch.object(
                app,
                "generate_preview_image",
                return_value=("preview-prompt", b"preview-bytes", "image/png"),
            ),
        ):
            preview = state.create_preview(board["id"], 1, True)
            for _ in range(100):
                preview = state.get_preview(preview["id"])
                if preview["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)
        self.assertEqual(preview["status"], "completed")
        self.assertEqual(
            state.preview_image(preview["id"]),
            (b"preview-bytes", "image/png"),
        )
        self.assertEqual(state.delete_preview(preview["id"]), {"deleted": True})
        self.assertNotIn(preview["id"], state.previews)

    def test_preview_result_emits_stable_identifier_and_age_free_prompt(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 1, "prompt_seed": 17, "inference_seed": 28}
        )
        with (
            patch.object(app, "load_workflow_runtime", return_value=({}, {})),
            patch.object(
                app, "generate_preview_image",
                return_value=("preview-prompt", b"preview-bytes", "image/png"),
            ),
            patch.object(app, "append_prompt_debug_record") as append,
        ):
            preview = state.create_preview(board["id"], 1, True)
            for _ in range(100):
                preview = state.get_preview(preview["id"])
                if preview["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)
        self.assertEqual(preview["status"], "completed")
        record = append.call_args.args[0]
        self.assertEqual(record["kind"], "shot_preview")
        self.assertEqual(record["result"], f"preview:{preview['id']}")
        self.assertNotRegex(record["positive"], r"\b(?:21|22|23)-year-old\b")
        self.assertEqual(record["lora_rules"], [])

    def test_completed_preview_images_have_independent_memory_entries(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 2, "prompt_seed": 71, "inference_seed": 72}
        )
        rendered = iter((b"studio-preview", b"director-preview"))

        def render_preview(*_args, **_kwargs):
            return "preview-prompt", next(rendered), "image/png"

        preview_ids = []
        with (
            patch.object(app, "load_workflow_runtime", return_value=({}, {})),
            patch.object(app, "generate_preview_image", side_effect=render_preview),
        ):
            for shot in (1, 2):
                preview = state.create_preview(board["id"], shot, True)
                preview_ids.append(preview["id"])
                for _ in range(100):
                    preview = state.get_preview(preview["id"])
                    if preview["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.01)

        self.assertNotEqual(*preview_ids)
        self.assertEqual(state.preview_image(preview_ids[0])[0], b"studio-preview")
        self.assertEqual(state.preview_image(preview_ids[1])[0], b"director-preview")
        state.delete_preview(preview_ids[1])
        self.assertEqual(state.preview_image(preview_ids[0])[0], b"studio-preview")
        self.assertNotIn(preview_ids[1], state.previews)

    def test_preview_always_uses_preview_workflow_without_toggle(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 1, "prompt_seed": 33, "inference_seed": 44}
        )
        with patch.object(app.threading, "Thread"):
            preview = state.create_preview(board["id"], 1, False)
        self.assertEqual(preview["type"], "preview")
        self.assertTrue(preview["positive"])
        self.assertTrue(preview["negative"])
        self.assertEqual(preview["seed"], board["shots"][0]["inference_seed"])


    def test_save_image_nodes_become_temporary_preview_nodes(self):
        class Response:
            headers = {"Content-Type": "image/png"}
            content = b"image"

            def raise_for_status(self):
                return None

            def json(self):
                return {"prompt_id": "prompt-id"}

        class Session:
            def __init__(self):
                self.queued_workflow = None

            def post(self, url, json, timeout):
                self.queued_workflow = json["prompt"]
                return Response()

            def get(self, url, params, timeout):
                return Response()

        session = Session()
        workflow = {
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "sampler": {"class_type": "KSampler", "inputs": {"seed": 1}},
            "save": {
                "class_type": "SaveImage",
                "inputs": {"images": ["sampler", 0], "filename_prefix": "Valhalla"},
            },
        }
        mapping = {
            "positive_prompt": {"node": "positive", "input": "text"},
            "negative_prompt": {"node": "negative", "input": "text"},
            "inference_seed": [{"node": "sampler", "input": "seed"}],
        }
        outputs = {"save": {"images": [{"filename": "preview.png"}]}}
        with (
            patch.object(app, "prepare_fast_workflow", side_effect=lambda graph, _: graph),
            patch.object(app, "comfy_session", return_value=(session, "http://comfy", 1)),
            patch.object(app, "wait_for_outputs", return_value=outputs),
        ):
            _, image, mime_type = app.generate_preview_image(
                {"settings": {}}, "positive", "negative", 9, workflow, mapping
            )
        self.assertEqual(image, b"image")
        self.assertEqual(mime_type, "image/png")
        self.assertEqual(session.queued_workflow["save"]["class_type"], "PreviewImage")
        self.assertNotIn("filename_prefix", session.queued_workflow["save"]["inputs"])

    def test_clear_logger_hides_preview_but_keeps_displayed_image_available(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 1, "prompt_seed": 55, "inference_seed": 66}
        )
        with patch.object(app.threading, "Thread"):
            preview = state.create_preview(board["id"], 1, False)
        stored = state.previews[preview["id"]]
        stored.update(
            status="completed", image_bytes=b"still-visible", mime_type="image/png"
        )
        result = state.clear_logger()
        self.assertEqual(result["previews"], 1)
        self.assertIsNone(state.jobs_payload()["latest_preview"])
        self.assertEqual(
            state.preview_image(preview["id"]), (b"still-visible", "image/png")
        )


class OutputDeletionRegressionTests(unittest.TestCase):
    def test_gallery_lists_png_jpg_and_jpeg_files_together(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for name in ("first.png", "second.jpg", "third.jpeg"):
                (output_dir / name).write_bytes(name.encode())
            (output_dir / "ignore.txt").write_text("not an image", encoding="utf-8")
            with patch.object(app, "proof_directories", return_value=[("output", output_dir)]):
                outputs = app.list_output_images()
            self.assertEqual({item["name"] for item in outputs}, {
                "first.png", "second.jpg", "third.jpeg",
            })

    def test_proofs_dirs_load_duplicate_names_and_delete_only_selected_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "outputs"
            archive_dir = root / "archive"
            output_dir.mkdir()
            archive_dir.mkdir()
            (output_dir / "same.png").write_bytes(b"current")
            output_nested = output_dir / "not-scanned"
            output_nested.mkdir()
            (output_nested / "hidden.png").write_bytes(b"live nested")
            (archive_dir / "same.png").write_bytes(b"archived")
            nested_dir = archive_dir / "older" / "session"
            nested_dir.mkdir(parents=True)
            (nested_dir / "same.png").write_bytes(b"nested archived")
            config, _ = app.load_config()
            config["storage"].update(
                output_dir=str(output_dir), proofs_dir=[str(archive_dir)]
            )
            config_file = root / "config.json"
            config_file.write_text(app.json.dumps(config), encoding="utf-8")

            with patch.object(app, "config_path", return_value=config_file):
                outputs = app.list_output_images()
                self.assertEqual(
                    {item["key"] for item in outputs},
                    {
                        "output:same.png", "proof-1:same.png",
                        "proof-1:older/session/same.png",
                    },
                )
                self.assertEqual(
                    {item["source"] for item in outputs}, {"output", "proof-1"}
                )
                self.assertEqual(
                    [item["source"] for item in outputs],
                    ["proof-1", "proof-1", "output"],
                )
                self.assertNotIn("output:not-scanned/hidden.png", {
                    item["key"] for item in outputs
                })
                nested = next(
                    item for item in outputs
                    if item["key"] == "proof-1:older/session/same.png"
                )
                self.assertIn("older%2Fsession%2Fsame.png", nested["url"])
                result = app.delete_output_image(nested["relative_path"], "proof-1")
                outside = root / "outside.png"
                outside.write_bytes(b"outside")
                with self.assertRaisesRegex(app.AppError, "Invalid proof path"):
                    app.delete_output_image("../outside.png", "proof-1")
                self.assertTrue(outside.is_file())

            self.assertEqual(result["source"], "proof-1")
            self.assertTrue((output_dir / "same.png").is_file())
            self.assertTrue((archive_dir / "same.png").is_file())
            self.assertFalse((nested_dir / "same.png").exists())

    def test_proofs_dir_accepts_one_directory_string(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "outputs"
            archive_dir = root / "archive"
            output_dir.mkdir()
            archive_dir.mkdir()
            (archive_dir / "archived.jpg").write_bytes(b"image")
            config, _ = app.load_config()
            config["storage"].update(
                output_dir=str(output_dir), proofs_dir=str(archive_dir)
            )
            config_file = root / "config.json"
            config_file.write_text(app.json.dumps(config), encoding="utf-8")

            with patch.object(app, "config_path", return_value=config_file):
                outputs = app.list_output_images()

            self.assertEqual([item["key"] for item in outputs], ["proof-1:archived.jpg"])

    def test_parent_proof_source_excludes_nested_live_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof_root = root / "proofs"
            output_dir = proof_root / "live"
            archive_dir = proof_root / "archive"
            output_dir.mkdir(parents=True)
            archive_dir.mkdir()
            (output_dir / "same.png").write_bytes(b"live")
            (output_dir / "nested").mkdir()
            (output_dir / "nested" / "hidden.png").write_bytes(b"live nested")
            (archive_dir / "same.png").write_bytes(b"archive")
            config, _ = app.load_config()
            config["storage"].update(
                output_dir=str(output_dir),
                proofs_dir=[str(proof_root), str(output_dir)],
            )
            config_file = root / "config.json"
            config_file.write_text(app.json.dumps(config), encoding="utf-8")

            with patch.object(app, "config_path", return_value=config_file):
                outputs = app.list_output_images()
                self.assertEqual(
                    [item["key"] for item in outputs],
                    ["proof-1:archive/same.png", "output:same.png"],
                )
                result = app.delete_all_output_images()

            self.assertEqual(result["deleted"], 2)
            self.assertFalse((output_dir / "same.png").exists())
            self.assertTrue((output_dir / "nested" / "hidden.png").is_file())
            self.assertFalse((archive_dir / "same.png").exists())

    def test_output_encoder_creates_jpeg_and_strips_exif(self):
        source = app.Image.new("RGB", (12, 8), "#7357d8")
        exif = app.Image.Exif()
        exif[0x010E] = "private metadata"
        buffer = app.BytesIO()
        source.save(buffer, format="PNG", exif=exif)
        encoded, suffix = app.encode_output_image(buffer.getvalue(), {
            "output_format": "jpeg", "jpeg_quality": 95, "strip_exif": True,
        })
        self.assertEqual(suffix, ".jpg")
        with app.Image.open(app.BytesIO(encoded)) as saved:
            self.assertEqual(saved.format, "JPEG")
            self.assertEqual(saved.size, (12, 8))
            self.assertFalse(saved.getexif())

        retained, _ = app.encode_output_image(buffer.getvalue(), {
            "output_format": "jpeg", "jpeg_quality": 95, "strip_exif": False,
        })
        with app.Image.open(app.BytesIO(retained)) as saved:
            self.assertEqual(saved.getexif().get(0x010E), "private metadata")

        jpg_alias, suffix = app.encode_output_image(buffer.getvalue(), {
            "output_format": "jpg", "jpeg_quality": 95, "strip_exif": True,
        })
        self.assertEqual(suffix, ".jpg")
        with app.Image.open(app.BytesIO(jpg_alias)) as saved:
            self.assertEqual(saved.format, "JPEG")

    def test_gallery_benchmark_creates_synthetic_records_without_copying_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first = output_dir / "first.png"
            second = output_dir / "second.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with (
                patch.object(app, "proof_directories", return_value=[("output", output_dir)]),
                patch.object(app, "GALLERY_BENCHMARK_COUNT", 2000),
                patch.object(app, "GALLERY_BENCHMARK_SOURCES", 10),
            ):
                outputs = app.list_output_images()
                self.assertEqual(len(outputs), 2000)
                self.assertEqual(len({item["name"] for item in outputs}), 2000)
                self.assertEqual(len({item["url"] for item in outputs}), 2)
                self.assertEqual(len({item["thumbnail_url"] for item in outputs}), 2000)
                self.assertIn("benchmark=2000", outputs[-1]["thumbnail_url"])
                self.assertEqual(sorted(path.name for path in output_dir.iterdir()), ["first.png", "second.png"])
                with self.assertRaisesRegex(app.AppError, "benchmark mode"):
                    app.delete_output_image(outputs[0]["name"])

    def test_gallery_benchmark_cli_defaults_to_two_thousand_records(self):
        args = app.build_parser().parse_args(["gallery-benchmark", "--no-browser"])
        self.assertEqual(args.command, "gallery-benchmark")
        self.assertEqual(args.count, 2000)

    def test_output_payload_provides_versioned_thumbnail_url(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"image")
            payload = app.output_payload(target)
        self.assertEqual(payload["key"], "output:result.png")
        self.assertNotIn("modified_at", payload)
        self.assertEqual(payload["url"], "/api/outputs/result.png?source=output")
        self.assertRegex(
            payload["thumbnail_url"],
            r"^/api/thumbnails/result\.png\?source=output&v=\d+$",
        )

    def test_generated_thumbnail_is_reused_from_memory_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"image")
            app._thumbnail_cache_remove()
            with (
                patch.object(app, "output_directory", return_value=Path(directory)),
                patch.object(app, "Image", None),
            ):
                key = ("output", target.name, target.stat().st_mtime_ns, target.stat().st_size)
                with app.THUMBNAIL_CACHE_LOCK:
                    app.THUMBNAIL_CACHE[key] = b"thumbnail"
                    app.THUMBNAIL_CACHE_BYTES = len(b"thumbnail")
                self.assertEqual(app.output_thumbnail(target.name), b"thumbnail")
            app._thumbnail_cache_remove()

    def test_concurrent_thumbnail_misses_share_one_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"image")
            app._thumbnail_cache_remove()
            workers = 8
            ready = threading.Barrier(workers)
            calls = 0
            calls_lock = threading.Lock()

            def generate(_target):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.05)
                return b"thumbnail"

            def request_thumbnail():
                ready.wait()
                return app.output_thumbnail(target.name)

            with (
                patch.object(app, "output_directory", return_value=Path(directory)),
                patch.object(app, "_generate_thumbnail", side_effect=generate),
                concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor,
            ):
                results = list(executor.map(lambda _: request_thumbnail(), range(workers)))
            self.assertEqual(results, [b"thumbnail"] * workers)
            self.assertEqual(calls, 1)
            app._thumbnail_cache_remove()

    def test_thumbnail_generation_failure_is_shared_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"image")
            app._thumbnail_cache_remove()
            workers = 4
            ready = threading.Barrier(workers)

            def fail(_target):
                time.sleep(0.05)
                raise app.AppError("thumbnail failed")

            def request_thumbnail():
                ready.wait()
                return app.output_thumbnail(target.name)

            with (
                patch.object(app, "output_directory", return_value=Path(directory)),
                patch.object(app, "_generate_thumbnail", side_effect=fail) as generate,
                concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor,
            ):
                futures = [executor.submit(request_thumbnail) for _ in range(workers)]
                for future in futures:
                    with self.assertRaisesRegex(app.AppError, "thumbnail failed"):
                        future.result()
                self.assertEqual(generate.call_count, 1)

            with (
                patch.object(app, "output_directory", return_value=Path(directory)),
                patch.object(app, "_generate_thumbnail", return_value=b"retry") as generate,
            ):
                self.assertEqual(app.output_thumbnail(target.name), b"retry")
                generate.assert_called_once_with(target)
            app._thumbnail_cache_remove()

    def test_thumbnail_cache_evicts_least_recently_used_entries_by_byte_size(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first = output_dir / "first.png"
            second = output_dir / "second.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            generated = []

            def generate(target):
                generated.append(target.name)
                return target.stem.encode()[:6]

            app._thumbnail_cache_remove()
            with (
                patch.object(app, "output_directory", return_value=output_dir),
                patch.object(app, "_generate_thumbnail", side_effect=generate),
                patch.object(app, "thumbnail_cache_max_bytes", return_value=10),
            ):
                self.assertEqual(app.output_thumbnail(first.name), b"first")
                self.assertEqual(app.output_thumbnail(second.name), b"second")
                self.assertEqual(app.output_thumbnail(second.name), b"second")
                self.assertEqual(app.output_thumbnail(first.name), b"first")
                self.assertEqual(generated, ["first.png", "second.png", "first.png"])
                self.assertLessEqual(app.THUMBNAIL_CACHE_BYTES, 10)
            app._thumbnail_cache_remove()

    def test_zero_thumbnail_cache_budget_does_not_retain_generated_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.png"
            target.write_bytes(b"image")
            app._thumbnail_cache_remove()
            with (
                patch.object(app, "output_directory", return_value=Path(directory)),
                patch.object(app, "_generate_thumbnail", return_value=b"thumbnail"),
                patch.object(app, "thumbnail_cache_max_bytes", return_value=0),
            ):
                self.assertEqual(app.output_thumbnail(target.name), b"thumbnail")
                self.assertEqual(app.THUMBNAIL_CACHE_BYTES, 0)
                self.assertFalse(app.THUMBNAIL_CACHE)
            app._thumbnail_cache_remove()

    def test_deleting_output_invalidates_all_cached_versions_of_its_thumbnail(self):
        state = app.WebState()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            target = output_dir / "result.png"
            target.write_bytes(b"image")
            removed_keys = [
                ("output", target.name, 1, 5),
                ("output", target.name, 2, 5),
            ]
            retained_key = ("output", "other.png", 1, 5)
            app._thumbnail_cache_remove()
            with app.THUMBNAIL_CACHE_LOCK:
                app.THUMBNAIL_CACHE[removed_keys[0]] = b"old"
                app.THUMBNAIL_CACHE[retained_key] = b"other"
                app.THUMBNAIL_CACHE[removed_keys[1]] = b"new"
                app.THUMBNAIL_CACHE_BYTES = 11

            with (
                patch.object(app, "WEB_STATE", state),
                patch.object(app, "output_directory", return_value=output_dir),
            ):
                app.delete_output_image(target.name)

            with app.THUMBNAIL_CACHE_LOCK:
                self.assertEqual(list(app.THUMBNAIL_CACHE), [retained_key])
                self.assertEqual(app.THUMBNAIL_CACHE_BYTES, len(b"other"))
            app._thumbnail_cache_remove()

    def test_completed_frame_can_be_deleted_while_batch_is_rendering(self):
        state = app.WebState()
        name = "run_photoshoot_001_shot_001_1_image_01.png"
        state.jobs["job"] = {
            "status": "running",
            "_run_id": "run",
            "outputs": [{"name": name, "url": f"/api/outputs/{name}"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / name
            target.write_bytes(b"complete image")
            with (
                patch.object(app, "WEB_STATE", state),
                patch.object(app, "output_directory", return_value=Path(directory)),
            ):
                result = app.delete_output_image(name)
        self.assertEqual(result["deleted"], name)
        self.assertFalse(target.exists())
        self.assertEqual(state.jobs["job"]["outputs"], [])

    def test_frame_still_being_written_remains_protected(self):
        state = app.WebState()
        name = "run_photoshoot_001_shot_002_2_image_01.png"
        state.jobs["job"] = {
            "status": "running", "_run_id": "run", "outputs": []
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / name
            target.write_bytes(b"partial image")
            with (
                patch.object(app, "WEB_STATE", state),
                patch.object(app, "output_directory", return_value=Path(directory)),
            ):
                with self.assertRaisesRegex(app.AppError, "still being written"):
                    app.delete_output_image(name)
                self.assertTrue(target.exists())


class FrontendContractTests(unittest.TestCase):
    def test_release_version_is_consistent_across_server_and_ui(self):
        html = (Path(app.__file__).parent / "client" / "client.html").read_text(
            encoding="utf-8"
        )
        self.assertEqual(app.APP_VERSION, "1.5.2")
        self.assertEqual(app.ValhallaHandler.server_version, "Valhalla/1.5.2")
        self.assertIn('<span class="version">v1.5.2</span>', html)

    def test_primary_workspace_names_and_headers_use_photography_terms(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn('<span>Proofs</span>', html)
        self.assertIn('<span>Logbook</span>', html)
        self.assertIn('<title>Valhalla Photo Studio</title>', html)
        self.assertIn("studio: 'Photo Studio'", js)
        self.assertIn("outputs: 'Proof Gallery'", js)
        self.assertIn("logger: 'Production Logbook'", js)
        self.assertIn('id="view-eyebrow"', html)
        self.assertNotIn('>Render logger</h2>', html)

    def test_launcher_confirms_before_stopping_only_this_project_server(self):
        launcher = (Path(app.__file__).parent / "launcher.sh").read_text(encoding="utf-8")
        self.assertIn("is_project_server()", launcher)
        self.assertIn('grep -Fqx "$SERVER"', launcher)
        self.assertIn('[ "$process_cwd" = "$SCRIPT_DIR" ]', launcher)
        self.assertIn("Stop these processes before starting a new server? [y/N]", launcher)
        self.assertIn('kill -TERM "$process_id"', launcher)
        self.assertIn('kill -KILL "$process_id"', launcher)
        self.assertIn("if [ ! -t 0 ]", launcher)
        self.assertIn('exec "$PYTHON_BIN" "$SERVER" "$1"', launcher)

    def test_storyboard_transfer_is_studio_only_and_workflows_are_in_system(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn('id="studio-topbar-actions"', html)
        self.assertIn('id="studio-files-menu"', html)
        self.assertIn("name !== 'studio'", js)
        menu = html.split('id="studio-files-menu"', 1)[1].split('</details>', 1)[0]
        self.assertIn('id="import-storyboard"', menu)
        self.assertIn('id="export-storyboard" disabled', menu)
        self.assertNotIn('id="capture-button"', menu)
        system = html.split('id="system-card"', 1)[1].split('</section>', 1)[0]
        self.assertIn('id="capture-button"', system)
        self.assertNotIn('id="export-storyboard"', html.split('id="director-view"', 1)[1])
        self.assertIn("studioFilesMenu.open = false", js)
        self.assertIn("$('#system-settings').open = false", js)

    def test_active_render_accepts_additional_fifo_jobs(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("button.textContent = idleLabel", js)
        self.assertIn("alreadyActive ? 'Added to render queue'", js)
        self.assertIn("queuedJob.queue_position", js)
        self.assertIn("session.active_job.id !== job.id", js)

    def test_active_render_progress_dock_is_visible_inside_open_lightbox(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("function syncJobDockLayer()", js)
        self.assertIn("shell.append(jobDock)", js)
        self.assertIn("jobDock.classList.add('in-lightbox')", js)
        self.assertIn("document.body.append(jobDock)", js)
        self.assertIn("syncJobDockLayer();", js)

    def test_system_status_refreshes_while_the_browser_is_active(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("function scheduleStatusRefresh()", js)
        self.assertIn("statusRefreshSeconds * 1000", js)
        self.assertIn("if (document.hidden) return", js)
        self.assertIn("document.addEventListener('visibilitychange'", js)
        self.assertIn("statusRefreshActive", js)
        self.assertIn("status.comfy.refresh_seconds", js)

    def test_logger_timeline_can_inspect_historical_shot_prompts(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        server = (root / "server.py").read_text(encoding="utf-8")
        self.assertIn('id="logger-shot-label"', html)
        self.assertIn('id="logger-rendered-open"', html)
        self.assertIn('id="logger-rendered-image"', html)
        self.assertIn('id="logger-positive"', html)
        self.assertIn('id="logger-negative"', html)
        self.assertIn("function inspectedJobPrompt(job)", js)
        self.assertIn("function renderLoggerImage(prompt)", js)
        self.assertIn("renderLoggerImage(inspectedPrompt)", js)
        self.assertIn("loggerRenderedFrame.addEventListener('click'", js)
        self.assertIn("function sizeLoggerImageColumn()", js)
        self.assertIn("imageHeight * image.naturalWidth / image.naturalHeight", js)
        self.assertIn("new ResizeObserver(sizeLoggerImageColumn).observe($('.logger-prompt-grid'))", js)
        self.assertIn("function openLogbookImagePreview(prompt)", js)
        self.assertIn("persistent: true", js)
        self.assertIn("state.previewWindowSessions.logger", js)
        self.assertIn("loggerPreview.displayed.image_url = prompt.image_url", js)
        self.assertIn("if (!preview?.id || preview.persistent) return", js)
        self.assertIn("function rememberShotPreviewGeometry()", js)
        self.assertIn("function applyShotPreviewGeometry()", js)
        self.assertIn("previewWindowGeometryReady", js)
        self.assertIn(".shot-preview-canvas img { display: block; width: 100%; height: 100%; object-fit: contain; }", css)
        self.assertIn("$('#shot-preview-image').addEventListener('error', closeShotPreview)", js)
        self.assertIn("previewWindowSessions", js)
        self.assertIn("function loadPreviewWindowSessions()", js)
        self.assertIn("valhalla-floating-previews", js)
        self.assertIn("function persistPreviewWindowSessions()", js)
        self.assertIn("function suspendFloatingPreview()", js)
        self.assertIn("function restoreFloatingPreview(owner)", js)
        self.assertIn("if (name !== 'outputs') restoreFloatingPreview(name)", js)
        self.assertIn("state.previewJobOwner = activeViewName()", js)
        self.assertIn('data-log-index="${logIndex}"', js)
        self.assertIn("function inspectLoggerEvent(element)", js)
        self.assertIn("displayedLoggerPrompt()", js)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
        self.assertIn(".logger-rendered-frame.has-image", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("var(--logger-image-column) repeat(2, minmax(0, 1fr))", css)
        self.assertIn(".logger-event.inspectable.selected", css)
        self.assertIn('"image_url": shot_outputs[0]["url"] if shot_outputs else None', server)

    def test_gallery_benchmark_is_read_only_and_reports_bounded_dom_size(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("state.galleryBenchmark = Boolean(result.benchmark)", js)
        self.assertIn("outputGrid.childElementCount", js)
        self.assertIn("state.outputs.length === 0 || state.galleryBenchmark", js)
        self.assertIn("if (state.galleryBenchmark)", js)

    def test_sfw_is_a_structural_content_mode(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn('name="content" value="sfw"', html)
        self.assertIn("content_mode: value('content')", js)
        self.assertIn("content !== 'progressive'", js)
        self.assertNotIn("xxx_only:", js)

    def test_lightbox_close_aligns_grid_to_last_viewed_output(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("function syncOutputGridToPreview()", js)
        self.assertIn("focusOutputCard(state.previewIndex, { alignTop: true })", js)
        self.assertIn("window.scrollTo({ top: cardTop, behavior: 'auto' })", js)
        self.assertIn("syncOutputGridToPreview();", js)

    def test_lightbox_has_fullscreen_and_non_interrupting_slideshow_controls(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        self.assertIn('id="image-true-fullscreen"', html)
        self.assertIn('id="image-viewer-shell"', html)
        self.assertIn('id="image-slideshow-toggle"', html)
        self.assertNotIn("<span>Pause</span>", html)
        self.assertIn('class="viewer-delay-menu"', html)
        slideshow = html.split('id="image-slideshow-delay"', 1)[1].split("</details>", 1)[0]
        self.assertEqual(slideshow.count('data-slideshow-delay="'), 10)
        self.assertIn('class="slideshow-delay-popover"', slideshow)
        self.assertIn('id="slideshow-random"', slideshow)
        self.assertIn("function scheduleSlideshow()", js)
        self.assertIn("function movePreviewRandom()", js)
        self.assertIn("valhalla-slideshow-random", js)
        self.assertIn("if (state.slideshowActive) scheduleSlideshow();", js)
        self.assertIn("document.fullscreenElement === target", js)
        self.assertIn("target.requestFullscreen()", js)
        self.assertIn("const target = $('#image-viewer-shell')", js)
        self.assertIn("function hideFullscreenControls()", js)
        self.assertIn("setTimeout(hideFullscreenControls, 2200)", js)
        self.assertIn("event.clientY > 90", js)
        self.assertIn("function isViewerFullscreen()", js)
        self.assertIn("function setFallbackFullscreen(active)", js)
        self.assertIn("target.classList.contains('fallback-fullscreen')", js)
        self.assertIn("setFallbackFullscreen(true)", js)
        self.assertIn("event.code === 'Space' && !event.repeat && !isEditing", js)
        self.assertIn("toggleSlideshow();", js)
        self.assertIn("function fitPreviewMedia()", js)
        self.assertIn("video.videoWidth", js)
        self.assertIn("video.style.removeProperty('height')", js)
        self.assertIn(".image-stage video { position: absolute; left: 50%; top: 50%;", css)
        self.assertIn("width: auto; height: auto; max-width: none; max-height: none;", css)
        self.assertIn("background: transparent", css)

    def test_lightbox_supports_touch_pinch_zoom(self):
        root = Path(app.__file__).parent
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        self.assertIn("function touchDistance(touches)", js)
        self.assertIn("imageStage.addEventListener('touchstart'", js)
        self.assertIn("imageStage.addEventListener('touchmove'", js)
        self.assertIn("function renderPreviewPinch()", js)
        self.assertIn("requestAnimationFrame(renderPreviewPinch)", js)
        self.assertIn("--preview-pinch-scale", js)
        self.assertIn("setPreviewZoom(finalZoom)", js)
        self.assertIn("{ passive: false }", js)
        self.assertIn(".image-viewer-shell.fallback-fullscreen", css)
        self.assertIn(".image-stage.pinching img", css)
        self.assertIn("env(safe-area-inset-top)", css)

    def test_lightbox_captures_ios_swipes_without_confusing_vertical_motion(self):
        root = Path(app.__file__).parent
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        self.assertIn("let previewTouch = null", js)
        self.assertIn("event.pointerType === 'touch'", js)
        self.assertIn("function finishPreviewTouch(event)", js)
        self.assertIn("pannable: !state.previewFit && (bounds.x > 0 || bounds.y > 0)", js)
        self.assertIn("event.type === 'touchend'", js)
        self.assertIn("Math.abs(dx) > Math.abs(dy) * 1.2", js)
        self.assertIn("if (swiped)", js)
        self.assertIn(".image-stage { touch-action: none; }", css)
        self.assertIn(".viewer-zoom input { display: none; }", css)
        self.assertIn("grid-template-rows: 54px minmax(0, 1fr)", css)

    def test_output_cards_use_lazy_async_thumbnails(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("item.thumbnail_url || item.url", js)
        self.assertIn('loading="lazy" decoding="async"', js)

    def test_job_polling_does_not_rebuild_unchanged_proofs(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        add_outputs = js.split("function addOutputs(outputs)", 1)[1].split(
            "function outputIdentity", 1
        )[0]
        self.assertIn("let added = false", add_outputs)
        self.assertIn("added = true", add_outputs)
        self.assertIn("if (!added) return false", add_outputs)
        self.assertIn("renderOutputs();", add_outputs)

    def test_render_queue_uses_noninteractive_placeholder_thumbnails_and_local_eta(self):
        root = Path(app.__file__).parent
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        self.assertIn("function syncJobPendingGroups(job", js)
        self.assertIn("job.pending_groups", js)
        self.assertIn("function pendingGroupCount(group)", js)
        self.assertIn("function pendingGroupSignature(group)", js)
        self.assertIn("function groupEntryAt(group, index)", js)
        self.assertIn("function flatEntryAt(index)", js)
        self.assertIn("pendingOutput(group.pendingGroup", js)
        self.assertIn("if (item.queue_group_key)", js)
        self.assertIn("item.queue_job_id === job.id", js)
        self.assertIn("data-pending-deadline", js)
        self.assertIn("function refreshPendingCountdowns()", js)
        self.assertIn("setInterval(refreshPendingCountdowns, 1000)", js)
        self.assertIn("ETA estimating", js)
        self.assertIn("`Shot ${displayShot} (${tier.toLowerCase()})`", js)
        self.assertIn("rendering ? (deadline ? `ETA ${formatDuration(item.eta_seconds)}`", js)
        self.assertIn(": 'Queued'", js)
        self.assertIn("pendingGroup.group_eta_seconds", js)
        self.assertIn("function modeTitle(mode)", js)
        self.assertIn("function tierTitle(tier)", js)
        self.assertIn("completionDeadline", js)
        self.assertIn("if (!item || item.pending) return", js)
        self.assertIn("if (card.classList.contains('pending-output')) return", js)
        pending_card = js.split("if (item.pending) {", 1)[1].split(
            "const visual = state.privacyCovered", 1
        )[0]
        self.assertNotIn("/api/thumbnails/", pending_card)
        self.assertNotIn("download=", pending_card)
        self.assertNotIn("data-action=\"delete-output\"", pending_card)
        self.assertNotIn("<i>", pending_card)
        self.assertNotIn("<span>Waiting</span>", pending_card)
        self.assertIn(".output-card.pending-output", css)
        self.assertNotIn("@keyframes pending-spin", css)
        self.assertNotIn(".render-placeholder i", css)

    def test_privacy_cover_is_persistent_high_priority_and_releases_image_sources(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn('data-privacy-shortcut="middle"', html)
        self.assertIn('<summary><span>Options</span>', html)
        self.assertIn('data-privacy-shortcut="shift-x"', html)
        self.assertIn('data-privacy-shortcut="both"', html)
        self.assertIn('Double middle click to reveal', html)
        for idle in ("0", "5", "15"):
            self.assertIn(f'data-privacy-idle="{idle}"', html)
        self.assertEqual(html.count('data-privacy-idle-option="'), 2)
        self.assertIn("localStorage.getItem('valhalla-privacy-covered')", js)
        self.assertIn("localStorage.setItem('valhalla-privacy-covered'", js)
        self.assertIn("image.removeAttribute('srcset')", js)
        self.assertIn("image.removeAttribute('src')", js)
        self.assertIn("stopSlideshow();", js)
        self.assertIn("event.stopImmediatePropagation();", js)
        self.assertIn("{ capture: true, passive: false }", js)
        self.assertIn("const PRIVACY_UNLOCK_DOUBLE_CLICK_MS = 500", js)
        self.assertIn("now - privacyMiddleClickAt <= PRIVACY_UNLOCK_DOUBLE_CLICK_MS", js)
        self.assertIn("applyPrivacyCover(true)", js)
        self.assertIn("applyPrivacyCover(false)", js)
        self.assertIn("localStorage.getItem('valhalla-privacy-idle-minutes')", js)
        self.assertIn("function schedulePrivacyIdleCover()", js)
        self.assertIn("window.addEventListener('pointermove', notePrivacyActivity", js)
        self.assertIn("if (promptDialog.open) promptDialog.close()", js)
        self.assertIn("$('#image-viewer-title').textContent = 'Preview'", js)
        self.assertIn("state.privacyCovered ? 'Preview' : item.name", js)
        self.assertIn("state.privacyCovered", js)
        self.assertIn(".privacy-covered img", css)
        self.assertIn(".privacy-placeholder", css)
        self.assertIn(".privacy-covered .privacy-control", css)
        self.assertIn(".privacy-covered .system-settings > summary", css)
        self.assertIn(".privacy-covered .logger-prompt pre", css)
        self.assertIn(".privacy-covered .logger-prompt::after", css)
        self.assertIn(".privacy-covered .image-stage video", css)
        self.assertIn("visibility: hidden !important", css)
        self.assertIn("!button || !prompt || state.privacyCovered", js)
        self.assertGreaterEqual(css.count('content: "⊝"'), 3)
        self.assertNotIn('content: "Image covered"', css)

    def test_lightbox_fit_is_enabled_by_default_for_new_sessions(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("sessionStorage.getItem('valhalla-preview-fit') !== 'false'", js)
        self.assertIn("(max-width: 560px) and (orientation: portrait)", js)
        self.assertIn("stageHeight / mediaHeight", js)

    def test_output_gallery_virtualizes_rows_with_bounded_overscan(self):
        root = Path(app.__file__).parent
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        self.assertIn("const OUTPUT_OVERSCAN_ROWS = 3", js)
        self.assertIn("const OUTPUT_VIRTUALIZATION_THRESHOLD = 100", js)
        self.assertIn("entryCount <= OUTPUT_VIRTUALIZATION_THRESHOLD", js)
        self.assertIn(".slice(start, end)", js)
        self.assertIn("renderVirtualOutputs", js)
        self.assertIn("outputGrid.classList.add('virtualized')", js)
        self.assertIn("repeat(var(--output-columns), var(--output-card-width))", css)
        self.assertIn("outputGrid.style.paddingTop", js)
        self.assertNotIn("position: absolute; inset: 0 auto auto 0", css)
        self.assertIn("var(--output-card-width, 230px)", css)
        self.assertNotIn("state.outputs.map((item, index)", js)

    def test_output_gallery_thumbnail_size_is_persistent_and_modifier_scoped(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn('id="gallery-size" type="range"', html)
        self.assertIn("localStorage.getItem('valhalla-gallery-thumbnail-size')", js)
        self.assertIn("storedGalleryCardSize !== null", js)
        self.assertIn("localStorage.setItem('valhalla-gallery-thumbnail-size'", js)
        self.assertIn("if (!(event.ctrlKey || event.metaKey)) return", js)
        self.assertIn("window.addEventListener('wheel'", js)
        self.assertIn("if (!outputGrid.contains(event.target)) return", js)
        self.assertIn("}, { passive: false })", js)
        self.assertIn("function galleryViewportAnchor()", js)
        self.assertIn("anchor.index / layout.columns", js)
        self.assertIn("var(--output-card-width, 230px)", css)

    def test_output_gallery_groups_photoshoots_by_run_and_set_filename(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")

        self.assertIn('data-gallery-view="flat"', html)
        self.assertIn('data-gallery-view="photoshoots"', html)
        self.assertIn('>Motion</button>', html)
        self.assertIn('>All proofs</button>', html)
        self.assertNotIn('>Videos</button>', html)
        self.assertNotIn('>All media</button>', html)
        self.assertNotIn('>All images</button>', html)
        self.assertLess(html.index('data-gallery-view="photoshoots"'), html.index('data-gallery-view="videos"'))
        self.assertLess(html.index('data-gallery-view="videos"'), html.index('data-gallery-view="flat"'))
        self.assertIn("const OUTPUT_FILENAME", js)
        self.assertIn("(photoshoot|random)", js)
        self.assertIn("(production|preview)", js)
        self.assertIn("kind: match[2]", js)
        self.assertIn("tier: match[4]", js)
        self.assertIn("group.displayNumber = ++randomNumber", js)
        self.assertIn("group.displayNumber = ++photoshootNumber", js)
        self.assertIn("`Random ${group.displayNumber}`", js)
        self.assertIn("`Photoshoot ${group.displayNumber}`", js)
        self.assertNotIn("const LEGACY_RUN_FILENAME", js)
        self.assertIn("sessionStorage.getItem('valhalla-gallery-view') === 'flat' ? 'flat' : 'photoshoots'", js)
        self.assertIn("function formatOutputRun(run)", js)
        self.assertIn("Render ID: ${group.identity.run}", js)
        self.assertIn("function photoshootGroups()", js)
        self.assertIn("function galleryMediaMatches(item)", js)
        self.assertIn("if (state.galleryView === 'flat') return true;", js)
        self.assertIn("function outputShotSequence(item)", js)
        self.assertIn("outputShotSequence(left.item) - outputShotSequence(right.item)", js)
        self.assertIn("function sortOutputsByFilename()", js)
        self.assertNotIn("modified_at", js)
        self.assertIn("function openPhotoshoot(key)", js)
        self.assertIn("function closePhotoshoot()", js)
        self.assertIn("if (event.key !== 'Escape' || !state.galleryGroup) return", js)
        self.assertIn("rememberProofsPosition();", js)
        self.assertIn("restoreProofsPosition({ fallbackToGrid: true });", js)
        self.assertIn("event.stopPropagation();", js)
        self.assertIn("sessionStorage.setItem('valhalla-gallery-view', next)", js)
        self.assertIn("state.flatScrollY = window.scrollY", js)
        self.assertIn("activePhotoshootGroup()?.items", js)
        self.assertIn("function outputDisplayShot(item, group = null)", js)
        self.assertIn("['photoshoot', 'random'].includes(group?.identity?.kind)", js)
        self.assertIn("const localShot = outputShotSequence(item)", js)
        self.assertIn("outputCardHtml(entry.item, entry.outputIndex, layout, index, group)", js)
        self.assertIn("aria-posinset=\"${position + 1}\"", js)

    def test_bulk_delete_is_scoped_to_the_opened_photoshoot(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("const photoshootList = state.galleryView === 'photoshoots' && !group", js)
        self.assertIn("group?.identity?.kind === 'random' ? 'random group' : 'photoshoot'", js)
        self.assertIn("group ? `Delete ${groupLabel}` : 'Delete all'", js)
        self.assertIn("group.items.map(({ item }) => item)", js)
        self.assertIn("targets.map((item) => api(item.url, { method: 'DELETE' }))", js)
        self.assertIn("Only the opened ${groupLabel} will be permanently deleted", js)
        self.assertIn("api('/api/outputs', { method: 'DELETE' })", js)

    def test_outputs_use_orthogonal_mode_and_tier_filenames(self):
        server = (Path(app.__file__).parent / "server.py").read_text(encoding="utf-8")
        self.assertIn('f"{mode}_{group_index:03d}_{render_tier}_"', server)
        self.assertIn('f"shot_{shot_index + 1:03d}"', server)
        self.assertNotIn('label = f"preview_', server)

    def test_virtual_output_navigation_uses_stable_absolute_indexes(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn('data-output-index="${index}"', js)
        self.assertIn("focusOutputCard(state.previewIndex, { alignTop: true })", js)
        self.assertIn("ArrowUp: -columns, ArrowDown: columns", js)

    def test_image_preview_supports_horizontal_and_vertical_arrow_navigation(self):
        js = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("['ArrowLeft', 'ArrowUp'].includes(event.key)", js)
        self.assertIn("['ArrowRight', 'ArrowDown'].includes(event.key)", js)

    def test_narrow_layout_keeps_all_pages_and_system_controls_accessible(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        for view in ("studio", "director", "outputs", "logger"):
            self.assertIn(f'data-view="{view}"', html)
        self.assertIn('id="mobile-system-toggle"', html)
        self.assertIn('aria-controls="system-card"', html)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", css)
        self.assertIn(".system-card.mobile-open { display: block; }", css)
        self.assertIn("const isOpen = systemCard.classList.toggle('mobile-open')", js)
        self.assertIn("if (event.key === 'Escape') closeMobileSystem()", js)

    def test_iphone_layout_has_native_touch_targets_and_stable_action_rows(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn("viewport-fit=cover", html)
        self.assertIn("/* iPhone/mobile refinement", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("font-size: 16px", css)
        self.assertIn("grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr)", css)
        self.assertIn(".director-quick-actions::-webkit-scrollbar", css)
        self.assertIn("grid-template-rows: auto auto auto", css)
        self.assertIn("grid-template-columns: 30px minmax(0, 1fr) auto", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        self.assertIn("@media (hover: none) and (pointer: coarse)", css)
        self.assertIn("width: min(288px, calc(100vw - 16px))", css)
        self.assertIn(".system-card .system-settings > summary { justify-content: space-between; }", css)


    def test_typography_presets_use_relative_scale_with_normal_default(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        for size in ("small", "normal", "large"):
            self.assertIn(f'data-type-size="{size}"', html)
            self.assertIn(f':root[data-type-size="{size}"]', css)
        self.assertIn(": 'normal'", js)
        self.assertIn("valhalla-type-size", js)
        self.assertIn("font-size: 0.75rem", css)

    def test_accent_switcher_offers_three_session_scoped_palettes(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")
        for accent in ("lavender", "azure", "rose"):
            self.assertIn(f'data-accent="{accent}"', html)
        self.assertEqual(html.count('system-choice-control'), 5)
        for theme in ("system", "light", "dark"):
            self.assertIn(f'data-theme-choice="{theme}"', html)
        self.assertIn("sessionStorage.setItem('valhalla-accent', accent)", js)
        self.assertIn("sessionStorage.setItem('valhalla-theme', state.theme)", js)
        self.assertIn("function applyAccent()", js)
        self.assertIn(".system-choice-control button.active", css)
        self.assertIn(".system-choice-control:not(.accent-control) button.active", css)
        self.assertIn(':root[data-accent="azure"]', css)
        self.assertIn(':root[data-accent="rose"]', css)
        for semantic in ("success", "warning", "danger"):
            self.assertIn(
                f"--{semantic}: color-mix(in oklab, var(--{semantic}-base) 90%, var(--brand))",
                css,
            )
        self.assertIn("accent-color: var(--brand)", css)

    def test_entity_values_are_capitalized_without_bold_emphasis(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "client" / "client.js").read_text(encoding="utf-8")
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn("function displayValue(value)", js)
        self.assertIn("escapeHtml(displayValue(shot.pose.prompt))", js)
        self.assertIn("const display = displayValue(value)", js)
        self.assertIn('strong title="${escapeHtml(display)}"', js)
        self.assertIn(".shot-detail strong { overflow: hidden; color: var(--text); font-weight: 450;", css)
        self.assertIn(".director-summary strong { margin-top: 3px; font-size: 0.75rem; font-weight: 450;", css)

    def test_clearing_director_search_collapses_all_groups(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "client" / "client.js").read_text(encoding="utf-8")

        self.assertIn("filterDirector(query, { collapseEmpty = false } = {})", js)
        self.assertIn("if (!normalized && collapseEmpty)", js)
        self.assertIn("state.directorOpenGroup = null", js)
        self.assertIn("filterDirector(event.target.value, { collapseEmpty: true })", js)

    def test_director_custom_value_can_select_propagation_scope(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        js = (root / "client" / "client.js").read_text(encoding="utf-8")

        self.assertIn('id="director-custom-propagation"', html)
        self.assertIn('value="all"', html)
        self.assertIn("custom_scope: customScope", js)
        self.assertIn('isRandom ? "All shots" : "All sets"', js)

    def test_director_marks_defaults_only_inside_dropdown(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "client" / "client.js").read_text(encoding="utf-8")

        self.assertIn("option.default ? ' (default)' : ''", js)
        self.assertNotIn("Database default", js)
        self.assertNotIn("escapeHtml(current?.prompt || current?.label || '')", js)

    def test_dropdowns_and_render_split_share_unified_control_geometry(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn(".field input, .field select, .director-field select", css)
        self.assertIn(".render-choice { --render-color: var(--success);", css)
        self.assertIn(".render-mode-popover { position: absolute;", css)
        self.assertIn(".render-choice:focus-within", css)
        self.assertIn(".render-choice:hover .button.render", css)
        self.assertIn(".render-choice-menu > summary:hover { background: var(--render-hover); }", css)
        self.assertIn(".render-choice.preview { --render-color: var(--brand);", css)

    def test_studio_and_director_use_the_same_control_column_width(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "client" / "client.css").read_text(encoding="utf-8")

        self.assertIn("--workspace-control-column: 328px", css)
        self.assertIn(
            ".studio-grid { display: grid; grid-template-columns: var(--workspace-control-column)",
            css,
        )
        self.assertIn(
            ".director-workspace { min-height: calc(100vh - 122px); display: grid; grid-template-columns: var(--workspace-control-column)",
            css,
        )
        self.assertIn(".director-rail { position: static; width: 100%; min-width: 0;", css)
        self.assertNotIn(".director-rail { position: sticky", css)

    def test_storyboard_render_mode_uses_synchronized_split_buttons(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        javascript = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertNotIn('name="fast"', html)
        self.assertEqual(html.count('class="render-choice-menu" data-render-mode'), 2)
        self.assertEqual(html.count('data-render-mode-choice="preview"'), 2)
        self.assertIn("Faster draft workflow", html)
        self.assertIn("Render storyboard", html)
        self.assertIn("state.renderMode === 'preview'", javascript)
        self.assertNotIn("retry_count", html)
        self.assertNotIn("retry_count", javascript)

    def test_active_page_is_restored_after_browser_reload(self):
        javascript = (Path(app.__file__).parent / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("sessionStorage.getItem('valhalla-active-view')", javascript)
        self.assertIn("sessionStorage.setItem('valhalla-active-view', name)", javascript)
        self.assertIn("switchView(restoredView);", javascript)
        self.assertIn("function rememberProofsPosition()", javascript)
        self.assertIn("function restoreProofsPosition(", javascript)
        self.assertIn("valhalla-proofs-positions", javascript)
        self.assertIn("window.addEventListener('pagehide', rememberProofsPosition)", javascript)

    def test_global_settings_have_pending_update_and_slider_guardrails(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        javascript = (root / "client" / "client.js").read_text(encoding="utf-8")
        for marker in (
            'id="config-notice"', 'id="active-config"',
            'id="nsfw-help"', 'id="plateau-help"',
            'id="update-storyboard-dialog"',
        ):
            self.assertIn(marker, html)
        self.assertIn("state.pendingStructural", javascript)
        self.assertIn("Update & Render", javascript)
        self.assertIn("form.elements.plateau_percent.max = String(nsfw)", javascript)
        self.assertIn("form.elements.plateau_percent.disabled", javascript)
        self.assertIn("if (state.pendingStructural)", javascript)


class CameraGrammarRegressionTests(unittest.TestCase):
    def setUp(self):
        self.database, _ = app.load_database()
        self.index = {
            item["id"]: item for item in app.iter_content_items(self.database)
        }

    def explicit_scene(self, recipe_id):
        composer = app.Composer(self.database, app.random.Random(20260721))
        context = composer.fixed_context()
        stage = next(
            stage for stage in app.effective_photoshoot_stages(
                context["outfit"]["template"]
            )
            if stage["level"] == "explicit"
        )
        return composer.resolve_scene(
            context, stage, {"explicit_recipe": recipe_id}
        )

    def test_intimate_macro_rejects_environmental_framing_with_exact_ids(self):
        scene = self.explicit_scene("recipe_intimate_macro")
        scene["shot_size"] = self.index["shot_intimate_macro"]
        scene["framing"] = self.index["framing_environmental"]
        with self.assertRaisesRegex(
            app.AppError,
            r"Camera conflict \[shot_intimate_macro, framing_environmental\]",
        ):
            app.validate_camera_grammar(scene)

    def test_rear_recipe_requires_rear_angle_with_exact_ids(self):
        scene = self.explicit_scene("recipe_rear_standing")
        scene["camera_angle"] = self.index["angle_eye_level"]
        with self.assertRaisesRegex(
            app.AppError,
            r"Camera conflict \[recipe_rear_standing, angle_eye_level\]",
        ):
            app.validate_camera_grammar(scene)
        self.assertFalse(app.camera_candidate_compatible(
            self.explicit_scene("recipe_rear_standing"),
            "camera_angle",
            self.index["angle_eye_level"],
        ))

    def test_intimate_action_requires_intimate_focus_and_close_treatment(self):
        scene = self.explicit_scene("recipe_hands_only")
        scene["focus_target"] = self.index["focus_face"]
        with self.assertRaisesRegex(
            app.AppError,
            r"recipe_hands_only.*focus_face",
        ):
            app.validate_camera_grammar(scene)
        scene = self.explicit_scene("recipe_hands_only")
        scene["shot_size"] = self.index["shot_full_body"]
        with self.assertRaisesRegex(
            app.AppError,
            r"recipe_hands_only.*shot_full_body",
        ):
            app.validate_camera_grammar(scene)

    def test_deterministic_camera_stress_resolves_ten_thousand_scenes(self):
        result = app.camera_grammar_stress_test(self.database)
        enabled_recipes = {
            item["id"] for item in self.database["explicit_recipes"]
            if not item.get("disabled", False)
        }
        self.assertEqual(result["checked"], 10_000)
        self.assertEqual(set(result["recipes"]), enabled_recipes)
        self.assertGreater(result["camera_tuples"], 100)


class StoryboardEditStateTests(unittest.TestCase):
    def test_director_edit_flag_survives_export_import(self):
        state = app.WebState()
        board = state.create_storyboard({
            "mode": "photoshoot", "count": 4, "photoshoots": 1,
            "prompt_seed": 123, "inference_seed": 456,
        })
        self.assertFalse(board["director_edited"])
        updated = state.update_director(
            board["id"],
            {"shot": 1, "field": "shot.intensity", "value": "sensual"},
        )
        self.assertTrue(updated["summary"] is not None)
        self.assertTrue(state.storyboard_payload(state.get_storyboard(board["id"]))["director_edited"])
        imported = state.import_storyboard(state.export_storyboard(board["id"]))
        self.assertTrue(imported["director_edited"])


class RenderLifecycleRegressionTests(unittest.TestCase):
    def test_cancel_requested_is_exposed_to_the_ui(self):
        state = app.WebState()
        job = {
            "id": "job",
            "status": "running",
            "cancel_requested": True,
        }
        self.assertTrue(state.job_payload(job)["cancel_requested"])


class PreviewVisualAuditTests(unittest.TestCase):
    def test_balanced_profile_is_reproducible_and_uses_non_default_subjects(self):
        replay_seed = 20260721
        first = preview_qa.build_record(3, 8, replay_seed)
        second = preview_qa.build_record(3, 8, replay_seed)
        self.assertEqual(
            [shot["inference_seed"] for shot in first["shots"]],
            [shot["inference_seed"] for shot in second["shots"]],
        )
        for photoshoot in range(3):
            shots = first["shots"][photoshoot * 8:(photoshoot + 1) * 8]
            safe = sum(
                shot["stage"]["level"] not in app.NSFW_LEVELS for shot in shots
            )
            self.assertEqual((safe, len(shots) - safe), (4, 4))
            self.assertTrue(
                preview_qa._uses_non_default_subject(first, photoshoot * 8 + 1)
            )

    def test_review_jpeg_limit_is_100_kilobytes(self):
        self.assertEqual(preview_qa.MAX_REVIEW_BYTES, 100_000)

    def test_random_audit_seed_survives_incompatible_scene_remix(self):
        record = preview_qa.build_record(10, 8, 8788274676380310665)
        self.assertEqual(len(record["shots"]), 80)


class VisualCompatibilityRegressionTests(unittest.TestCase):
    def test_chest_coverage_distinguishes_opaque_sheer_and_open_lingerie(self):
        database, _ = app.load_database()
        for garment in database["garments"]["bra"]:
            wording = f"{garment['id']} {garment['prompt']}".casefold()
            if "open_cup" in wording or "open-cup" in wording or "quarter-cup" in wording:
                self.assertIn("open_cup", app.tags(garment), garment["id"])

        def compile_template(template_id, stage_id, seed, required_bra_tag=None):
            template = next(
                item for item in database["outfit_templates"]
                if item["id"] == template_id
            )
            composer = app.Composer(database, app.random.Random(seed))
            context = composer.fixed_context()
            for _ in range(200):
                outfit = composer.choose_outfit(template, context["interior"])
                if (
                    required_bra_tag is None
                    or required_bra_tag in app.tags(outfit["garments"]["bra"])
                ):
                    break
            else:
                self.fail(f"Could not select {required_bra_tag} for {template_id}")
            context["outfit"] = outfit
            stage = next(item for item in outfit["template"]["stages"] if item["id"] == stage_id)
            scene = composer.resolve_scene(context, stage)
            positive, _, _ = app.compile_scene(database, scene)
            return scene, positive.casefold()

        opaque, opaque_prompt = compile_template(
            "template_classic_lingerie_normal", "classic_lingerie_layered", 7001
        )
        self.assertIn("opaque cups form complete continuous uninterrupted fabric coverage", opaque_prompt)
        self.assertIn(
            opaque["human"]["breast_size"]["covered_prompt"].casefold(),
            opaque_prompt,
        )
        self.assertNotIn(
            opaque["human"]["breast_size"]["prompt"].casefold(), opaque_prompt
        )
        for term in ("visible nipples", "areola", "breasts visibly framed"):
            self.assertNotIn(term, opaque_prompt)

        sheer, sheer_prompt = compile_template(
            "template_pearl_boudoir", "pearl_boudoir_reveal", 7002, "sheer"
        )
        self.assertIn("fabric remains visibly between skin and camera", sheer_prompt)
        self.assertIn("without openings or protrusions", sheer_prompt)
        self.assertIn(
            sheer["human"]["breast_size"]["covered_prompt"].casefold(),
            sheer_prompt,
        )
        self.assertNotIn(
            sheer["human"]["breast_size"]["prompt"].casefold(), sheer_prompt
        )
        for phrase in (
            "breasts and nipples visibly framed", "soft anatomy visible through",
            "visible nipples", "small areolas", "medium areolas", "large areolas",
        ):
            self.assertNotIn(phrase, sheer_prompt)

        opened, open_prompt = compile_template(
            "template_revealing_boudoir", "revealing_boudoir_bra", 7003, "open_cup"
        )
        self.assertIn("open-cup lingerie composition", open_prompt)
    def test_every_enabled_garment_is_reachable_through_a_matching_outfit_recipe(self):
        database, _ = app.load_database()
        enabled_templates = [
            template for template in database["outfit_templates"]
            if not template.get("disabled", False)
        ]
        unreachable = []
        for section, garments in database["garments"].items():
            for garment in garments:
                if garment.get("disabled", False):
                    continue
                reachable = any(
                    rule["catalog"] == section
                    and app.garment_matches_template_slot(
                        database, template, slot, garment, "progressive"
                    )
                    for template in enabled_templates
                    for slot, rule in template["slots"].items()
                )
                if not reachable:
                    unreachable.append(f"{section}.{garment['id']}")
        self.assertEqual(unreachable, [])

    def test_every_enabled_garment_survives_automatic_category_preference(self):
        database, _ = app.load_database()
        reachable = set()
        for template in database["outfit_templates"]:
            if template.get("disabled", False):
                continue
            for slot, rule in template["slots"].items():
                candidates = [
                    garment for garment in database["garments"][rule["catalog"]]
                    if app.garment_matches_template_slot(
                        database, template, slot, garment, "progressive"
                    )
                ]
                candidates = app.prefer_catalog_category(
                    candidates, app.catalog_category(template)
                )
                reachable.update(garment["id"] for garment in candidates)

        unreachable = [
            f"{section}.{garment['id']}"
            for section, garments in database["garments"].items()
            for garment in garments
            if not garment.get("disabled", False) and garment["id"] not in reachable
        ]
        self.assertEqual(unreachable, [])

    def test_every_outfit_template_resolves_for_every_enabled_interior(self):
        database, _ = app.load_database()
        enabled_interiors = [
            item for item in database["interiors"] if not item.get("disabled", False)
        ]
        enabled_templates = [
            item for item in database["outfit_templates"] if not item.get("disabled", False)
        ]
        checked = 0
        sfw_checked = 0
        for template_index, template in enumerate(enabled_templates):
            for interior_index, interior in enumerate(enabled_interiors):
                composer = app.Composer(
                    database,
                    app.random.Random(100_000 + template_index * 1_000 + interior_index),
                )
                outfit = composer.choose_outfit(template, interior, "progressive")
                composer.validate_outfit_stage_coverage(outfit)
                checked += 1
                if app.template_supports_sfw(database, template):
                    sfw_outfit = composer.choose_outfit(template, interior, "sfw")
                    app.validate_sfw_outfit(sfw_outfit)
                    sfw_checked += 1
        self.assertEqual(checked, len(enabled_templates) * len(enabled_interiors))
        self.assertEqual(
            sfw_checked,
            sum(
                app.template_supports_sfw(database, template)
                for template in enabled_templates
            ) * len(enabled_interiors),
        )

    def test_every_director_outfit_recipe_is_selectable_and_stage_safe(self):
        database, _ = app.load_database()
        for template in database["outfit_templates"]:
            if template.get("disabled", False):
                continue
            state = app.WebState()
            board = state.create_storyboard({
                "mode": "photoshoot", "content_mode": "progressive",
                "count": 12, "photoshoots": 1,
                "prompt_seed": 424242, "inference_seed": 515151,
                "nsfw_percent": 50, "plateau_percent": 20,
            })
            state.update_director(board["id"], {
                "shot": 1, "field": "outfit.template", "value": template["id"],
            })
            record = state.get_storyboard(board["id"])
            outfit = record["shots"][0]["context"]["outfit"]
            self.assertEqual(outfit["template"]["id"], template["id"])
            record["composer"].validate_outfit_stage_coverage(outfit)
            fields = director_fields(state.director_payload(board["id"], 1))
            baseline = app.copy.deepcopy(record)
            for slot in ("bra", "panties"):
                field = fields.get(f"outfit.garments.{slot}")
                if field is None:
                    continue
                for option in field["options"]:
                    if not option["id"]:
                        continue
                    garment = next(
                        item for item in database["garments"][slot]
                        if item["id"] == option["id"]
                    )
                    self.assertTrue(app.garment_compatible_with_template_stages(
                        outfit["template"], slot, garment
                    ))
            for key, field in fields.items():
                if not key.startswith("outfit.garments."):
                    continue
                for option in field["options"]:
                    if option.get("current", False):
                        continue
                    state.storyboards[board["id"]] = app.copy.deepcopy(baseline)
                    state.update_director(board["id"], {
                        "shot": 1, "field": key, "value": option["id"],
                    })

    def test_random_scenes_obey_physical_and_wardrobe_contracts(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(20260721))
        checked = 0
        for _ in range(80):
            fixed = composer.fixed_context()
            for stage in app.effective_photoshoot_stages(fixed["outfit"]["template"]):
                scene = composer.resolve_scene(fixed, stage)
                checked += 1
                furniture_tags = app.tags(scene["furniture"])
                if scene["pose"]["id"].startswith("pose_bed_"):
                    self.assertIn("bed", furniture_tags)
                if scene["pose"]["id"] == "pose_sofa_sprawl":
                    self.assertIn("sofa", furniture_tags)
                if scene["pose"]["id"] == "pose_chair_reverse":
                    self.assertIn("chair", furniture_tags)
                self.assertLessEqual(
                    sum(app.hands_required(scene.get(key)) for key in ("pose", "action", "prop")),
                    2,
                )
                self.assertFalse(
                    "rear_focus" in app.tags(scene["shot_size"])
                    and scene["focus_target"]["id"] == "focus_face"
                )
        self.assertGreater(checked, 200)

    def test_sheer_covered_stage_uses_same_opaque_bra_in_prompt(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(991177))
        found = False
        for _ in range(300):
            fixed = composer.fixed_context()
            for stage in fixed["outfit"]["template"]["stages"]:
                visible_chest = [
                    fixed["outfit"]["garments"][slot]
                    for slot in stage.get("visible_slots", [])
                    if slot in {"upperwear", "full_body", "outerwear"}
                    and slot in fixed["outfit"]["garments"]
                ]
                if stage["level"] != "covered" or not any(
                    app.tags(item) & {"sheer", "transparent"} for item in visible_chest
                ) or "bra" not in fixed["outfit"]["garments"]:
                    continue
                found = True
                bra = fixed["outfit"]["garments"]["bra"]
                self.assertFalse(app.tags(bra) & {"sheer", "explicit"})
                scene = composer.resolve_scene(fixed, stage)
                positive, _, selected = app.compile_scene(database, scene)
                self.assertIn(bra["prompt"], positive)
                self.assertIn(bra["id"], selected)
                self.assertIn("same underlying", positive)
                break
            if found:
                break
        self.assertTrue(found)

    def test_lingerie_stage_uses_only_opaque_anatomy_neutral_bras(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(778899))
        checked = 0
        for _ in range(250):
            fixed = composer.fixed_context()
            for stage in fixed["outfit"]["template"]["stages"]:
                if stage["level"] != "lingerie" or "bra" not in stage.get("visible_slots", []):
                    continue
                if {"breasts", "nipples"} & set(stage.get("body_visibility", [])):
                    continue
                bra = fixed["outfit"]["garments"]["bra"]
                self.assertFalse(app.tags(bra) & {"explicit", "sheer", "transparent", "open_cup"})
                scene = composer.resolve_scene(fixed, stage)
                positive, _, _ = app.compile_scene(database, scene)
                visible_outer = [
                    fixed["outfit"]["garments"][slot]
                    for slot in ("upperwear", "full_body", "outerwear")
                    if slot in stage.get("visible_slots", [])
                    and slot in fixed["outfit"]["garments"]
                ]
                if any(app.tags(item) & {"sheer", "transparent"} for item in visible_outer):
                    self.assertIn("the bra entirely beneath the outer garment", positive)
                elif visible_outer:
                    self.assertIn("one fully opaque upper-body garment as the visible chest layer", positive)
                else:
                    self.assertIn("one fully opaque chest-covering lingerie garment", positive)
                for unsafe_concept in ("breast", "nipple", "areola", "bust"):
                    self.assertNotIn(unsafe_concept, positive.casefold())
                checked += 1
                break
            if checked >= 40:
                break
        self.assertGreaterEqual(checked, 40)

    def test_covered_prompt_avoids_nipple_like_raised_disc_instruction(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(4545))
        while True:
            fixed = composer.fixed_context()
            stage = next((
                item for item in fixed["outfit"]["template"]["stages"]
                if item["level"] == "covered"
            ), None)
            if stage:
                break
        positive, _, _ = app.compile_scene(database, composer.resolve_scene(fixed, stage))
        self.assertIn("clean smooth garment surface", positive)
        for unsafe_concept in ("breast", "nipple", "areola", "bust"):
            self.assertNotIn(unsafe_concept, positive.casefold())

    def test_panties_are_compiled_beneath_pantyhose_and_tights(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(314159))
        checked = 0
        for _ in range(500):
            fixed = composer.fixed_context()
            legwear = fixed["outfit"]["garments"].get("legwear")
            if not legwear:
                continue
            legwear_prompt = legwear["prompt"].casefold()
            if not (
                "pantyhose" in app.tags(legwear)
                or "pantyhose" in legwear_prompt
                or "tights" in legwear_prompt
            ):
                continue
            for stage in app.effective_photoshoot_stages(fixed["outfit"]["template"]):
                visible = set(stage.get("visible_slots", []))
                if not {"panties", "legwear"}.issubset(visible):
                    continue
                positive, negative, _ = app.compile_scene(
                    database, composer.resolve_scene(fixed, stage)
                )
                self.assertIn("panties are worn underneath", positive)
                self.assertIn("continuous outer layer over the panties", positive)
                self.assertIn("panties outside pantyhose", negative)
                checked += 1
                break
            if checked >= 10:
                break
        self.assertGreaterEqual(checked, 10)

    def test_long_legwear_is_rejected_with_jeans_and_trousers(self):
        database, _ = app.load_database()
        jeans = next(
            item for item in database["garments"]["lowerwear"]
            if item["id"] == "lower_blue_jeans"
        )
        knee_socks = next(
            item for item in database["garments"]["legwear"]
            if item["id"] == "legwear_white_kneesocks"
        )
        template = next(
            item for item in database["outfit_templates"]
            if {"lowerwear", "legwear"}.issubset(item["slots"])
        )
        with self.assertRaisesRegex(app.AppError, "long legwear cannot be composed"):
            app.validate_outfit_layers(database, {
                "template": template,
                "garments": {"lowerwear": jeans, "legwear": knee_socks},
            })

        composer = app.Composer(database, app.random.Random(884422))
        checked = 0
        for _ in range(2000):
            fixed = composer.fixed_context()
            lowerwear = fixed["outfit"]["garments"].get("lowerwear")
            legwear = fixed["outfit"]["garments"].get("legwear")
            if not app.lowerwear_covers_legs(lowerwear) or not legwear:
                continue
            self.assertFalse(app.legwear_extends_above_ankle(legwear))
            stage = next((
                item for item in fixed["outfit"]["template"]["stages"]
                if {"lowerwear", "legwear"}.issubset(item.get("visible_slots", []))
            ), None)
            if not stage:
                continue
            positive, _, _ = app.compile_scene(
                database, composer.resolve_scene(fixed, stage)
            )
            self.assertIn("ankle socks begin below the trouser hems", positive)
            checked += 1
            if checked >= 10:
                break
        self.assertGreaterEqual(checked, 10)

    def test_hosiery_support_modes_are_explicit_and_structurally_paired(self):
        database, _ = app.load_database()
        hosiery = [
            item for item in database["garments"]["legwear"]
            if item.get("support_mode")
        ]
        self.assertEqual(
            {item["support_mode"] for item in hosiery},
            {"waist_continuous", "self_supporting", "garter_required"},
        )
        garter_stocking = next(
            item for item in hosiery if item["support_mode"] == "garter_required"
        )
        pantyhose = next(
            item for item in hosiery if item["support_mode"] == "waist_continuous"
        )
        belt = next(
            item for item in database["garments"]["accessories"]
            if "hosiery_support_belt" in app.tags(item)
        )
        template = next(
            item for item in database["outfit_templates"]
            if item["id"] == "template_stockings_garter"
        )
        with self.assertRaisesRegex(app.AppError, "requires a compatible garter"):
            app.validate_outfit_layers(database, {
                "template": template, "garments": {"legwear": garter_stocking},
            })
        with self.assertRaisesRegex(app.AppError, "requires garter-supported"):
            app.validate_outfit_layers(database, {
                "template": template,
                "garments": {"legwear": pantyhose, "accessories": belt},
            })
        app.validate_outfit_layers(database, {
            "template": template,
            "garments": {"legwear": garter_stocking, "accessories": belt},
        })

    def test_each_hosiery_support_mode_compiles_its_physical_contract(self):
        database, _ = app.load_database()
        composer = app.Composer(database, app.random.Random(828282))
        expected = {
            "waist_continuous": "one continuous waist-supported hosiery garment",
            "self_supporting": "integrated stay-up band",
            "garter_required": "four straight aligned support straps",
        }
        observed = set()
        for _ in range(4000):
            fixed = composer.fixed_context()
            legwear = fixed["outfit"]["garments"].get("legwear")
            mode = (legwear or {}).get("support_mode")
            if mode not in expected:
                continue
            stage = next((
                item for item in app.effective_photoshoot_stages(
                    fixed["outfit"]["template"]
                ) if "legwear" in item.get("visible_slots", [])
            ), None)
            if not stage:
                continue
            positive, _, _ = app.compile_scene(
                database, composer.resolve_scene(fixed, stage)
            )
            self.assertIn(expected[mode], positive)
            observed.add(mode)
            if observed == set(expected):
                break
        self.assertEqual(observed, set(expected))

    def test_hosiery_support_mode_schema_rejects_missing_or_unknown_values(self):
        database, _ = app.load_database()
        for value in (None, "magic_suspension"):
            broken = copy.deepcopy(database)
            item = broken["garments"]["legwear"][0]
            if value is None:
                item.pop("support_mode")
            else:
                item["support_mode"] = value
            with self.assertRaisesRegex(app.AppError, "support_mode"):
                app.validate_database(broken)


class WorkflowProfileTests(unittest.TestCase):
    def test_lora_nodes_are_detected_by_configurable_filename(self):
        workflow = {
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "sampler": {
                "class_type": "KSampler",
                "inputs": {"positive": ["positive", 0], "seed": 1},
            },
            "innie": {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": "renamed-anatomy.safetensors",
                    "strength_model": 0.5,
                    "strength_clip": 1.0,
                },
            },
        }
        mapping = app.detect_node_mapping(workflow)
        self.assertEqual(mapping["loras"], [{
            "node": "innie",
            "lora_name": "renamed-anatomy.safetensors",
            "strength_inputs": ["strength_model", "strength_clip"],
        }])

    def test_per_shot_lora_rules_apply_only_to_matching_semantics(self):
        database, _ = app.load_database()
        state = app.WebState()
        board = state.create_storyboard({
            "mode": "photoshoot", "content_mode": "progressive", "count": 12,
            "photoshoots": 1, "prompt_seed": 9292, "inference_seed": 9393,
        })
        shots = state.get_storyboard(board["id"])["shots"]
        covered = next(
            shot["scene"] for shot in shots
            if "genitals" not in shot["stage"].get("body_visibility", [])
        )
        exposed = next(
            shot["scene"] for shot in shots
            if "genitals" in shot["stage"].get("body_visibility", [])
        )
        name = database["settings"]["workflow_lora_rules"][0]["lora_name"]
        base_workflow = {"innie": {"inputs": {
            "lora_name": name, "strength_model": 0.5, "strength_clip": 1.0,
        }}}
        mapping = {"loras": [{
            "node": "innie", "lora_name": name,
            "strength_inputs": ["strength_model", "strength_clip"],
        }]}

        covered_workflow = copy.deepcopy(base_workflow)
        applied = app.apply_workflow_lora_rules(
            covered_workflow, mapping, database, covered
        )
        self.assertEqual(covered_workflow["innie"]["inputs"]["strength_model"], 0.1)
        self.assertEqual(covered_workflow["innie"]["inputs"]["strength_clip"], 1.0)
        self.assertEqual([item["rule_id"] for item in applied], ["innie_when_genitals_covered"])

        exposed_workflow = copy.deepcopy(base_workflow)
        self.assertEqual(
            app.apply_workflow_lora_rules(
                exposed_workflow, mapping, database, exposed
            ),
            [],
        )
        self.assertEqual(exposed_workflow, base_workflow)

        renamed = copy.deepcopy(database)
        renamed["settings"]["workflow_lora_rules"][0]["lora_name"] = "renamed.safetensors"
        renamed_workflow = copy.deepcopy(base_workflow)
        self.assertEqual(
            app.apply_workflow_lora_rules(
                renamed_workflow, mapping, renamed, covered
            ),
            [],
        )
        self.assertEqual(renamed_workflow, base_workflow)

    def test_lora_rules_apply_fifo_and_ignore_runtime_misses(self):
        database, _ = app.load_database()
        state = app.WebState()
        board = state.create_storyboard({
            "content_mode": "sfw", "count": 1,
            "prompt_seed": 101, "inference_seed": 202,
        })
        scene = state.get_storyboard(board["id"])["shots"][0]["scene"]
        name = database["settings"]["workflow_lora_rules"][0]["lora_name"]
        configured = copy.deepcopy(database)
        configured["settings"]["workflow_lora_rules"].extend([
            {
                "id": "first", "lora_name": name,
                "when": {"body_visibility_none": ["genitals"]},
                "strength_model": 0.2,
            },
            {
                "id": "second", "lora_name": name,
                "when": {"body_visibility_none": ["genitals"]},
                "strength_model": 0.3,
            },
        ])
        app.validate_database(configured)
        workflow = {"node": {"inputs": {"strength_model": 0.5}}}
        mapping = {"loras": [{
            "node": "node", "lora_name": name,
            "strength_inputs": ["strength_model"],
        }]}
        applied = app.apply_workflow_lora_rules(workflow, mapping, configured, scene)
        self.assertEqual(workflow["node"]["inputs"]["strength_model"], 0.3)
        self.assertEqual(
            [item["rule_id"] for item in applied],
            ["innie_when_genitals_covered", "first", "second"],
        )

        self.assertEqual(
            app.apply_workflow_lora_rules({}, {"loras": []}, configured, scene),
            [],
        )

    def test_lora_rule_schema_rejects_unknown_matchers(self):
        database, _ = app.load_database()
        broken = copy.deepcopy(database)
        broken["settings"]["workflow_lora_rules"][0]["when"] = {
            "unknown_stage_property": ["covered"],
        }
        with self.assertRaisesRegex(app.AppError, "supported match fields"):
            app.validate_database(broken)

    def test_latest_comfy_workflow_ignores_valhalla_preview_history(self):
        external_workflow = {"external": {"class_type": "KSampler", "inputs": {}}}
        preview_workflow = {"preview": {"class_type": "PreviewImage", "inputs": {}}}
        history = {
            "external-id": {
                "status": {"completed": True, "status_str": "success", "messages": [["execution_success", {"timestamp": 10}]]},
                "outputs": {"save": {}},
                "prompt": [1, "external-id", external_workflow, {"client_id": "comfy-web"}],
            },
            "valhalla-id": {
                "status": {"completed": True, "status_str": "success", "messages": [["execution_success", {"timestamp": 20}]]},
                "outputs": {"preview": {}},
                "prompt": [2, "valhalla-id", preview_workflow, {"valhalla_origin": True, "client_id": "valhalla-test"}],
            },
        }

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return history

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with patch.object(app, "comfy_session", return_value=(Session(), "http://comfy", 1)):
            prompt_id, workflow = app.latest_comfy_workflow({"settings": {}})
        self.assertEqual(prompt_id, "external-id")
        self.assertEqual(workflow, external_workflow)

    def test_valhalla_prompt_requests_are_persistently_identifiable(self):
        request = app.valhalla_prompt_request({"node": {}})
        self.assertTrue(request["client_id"].startswith("valhalla-"))
        self.assertIs(request["extra_data"]["valhalla_origin"], True)

    def test_live_preview_snapshot_still_requires_fast_mapping(self):
        state = app.WebState()
        board = state.create_storyboard(
            {"count": 1, "prompt_seed": 81, "inference_seed": 82}
        )
        workflow = {"node": {"class_type": "Example", "inputs": {}}}
        with (
            patch.object(app, "workflow_source", return_value="live"),
            patch.object(app, "latest_comfy_workflow", return_value=("external-id", workflow)),
            patch.object(app, "detect_node_mapping", return_value={"fast_mode": {}}) as detect,
            patch.object(app.threading, "Thread"),
        ):
            preview = state.create_preview(board["id"], 1, True)
        detect.assert_called_once_with(workflow, include_fast=True)
        self.assertEqual(preview["workflow_source"], "live")
        self.assertEqual(preview["source_prompt_id"], "external-id")
        self.assertEqual(state.previews[preview["id"]]["_workflow_template"], workflow)

    def test_live_workflow_controls_disable_profile_selection(self):
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        javascript = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn('id="live-workflow-source"', html)
        self.assertNotIn('id="save-profile-selection"', html)
        self.assertIn("profiles.source === 'live'", javascript)
        self.assertIn("source: $('#live-workflow-source').checked ? 'live' : 'profiles'", javascript)
        self.assertIn("select.addEventListener('change', saveWorkflowProfileSelection)", javascript)

    def test_operational_settings_live_only_in_root_config(self):
        config, _ = app.load_config()
        database, _ = app.load_database()
        self.assertEqual(set(config), {"server", "comfy", "storage", "gallery", "interface", "limits"})
        self.assertEqual(set(config["server"]), {"host", "port"})
        self.assertEqual(
            set(config["storage"]),
            {
                "output_dir", "proofs_dir", "output_format", "jpeg_quality",
                "strip_exif", "prompt_debug_log",
            },
        )
        self.assertEqual(
            set(config["storage"]["prompt_debug_log"]), {"enabled", "path"}
        )
        self.assertEqual(
            set(config["gallery"]), {"thumbnail_cache_mb", "thumbnail_max_edge"}
        )
        self.assertEqual(set(config["interface"]), {"privacy"})
        self.assertEqual(
            set(config["interface"]["privacy"]), {"auto_cover_minutes"}
        )
        limit_settings = {"max_scene_attempts", "max_storyboards", "max_jobs", "max_previews"}
        self.assertEqual(set(config["limits"]), limit_settings)
        self.assertTrue(limit_settings.isdisjoint(database["settings"]))
        comfy_operational = {
            "url", "workflows_dir", "http_timeout_seconds",
            "status_timeout_seconds", "status_refresh_seconds", "poll_interval_seconds",
            "generation_timeout_seconds", "preview_max_edge", "profiles",
        }
        self.assertTrue(comfy_operational.issubset(config["comfy"]))
        self.assertTrue(comfy_operational.isdisjoint(database["settings"]))
        self.assertTrue(comfy_operational.isdisjoint(config))
        self.assertNotIn("comfy_url", config)

    def test_server_address_defaults_come_from_root_config(self):
        config, path = app.load_config()
        self.assertEqual(path.name, "config.json")
        self.assertIsInstance(config["server"]["host"], str)
        self.assertGreater(config["server"]["port"], 0)
        parser = app.build_parser()
        arguments = parser.parse_args([])
        self.assertIsNone(arguments.host)
        self.assertIsNone(arguments.port)

    def test_zeroed_negative_conditioning_needs_only_one_text_prompt(self):
        workflow = {
            "text": {"class_type": "CLIPTextEncode", "inputs": {"text": "prompt", "clip": ["clip", 0]}},
            "zero": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["text", 0]}},
            "latent": {"class_type": "EmptyLatentImage", "inputs": {}},
            "sampler": {"class_type": "KSampler", "inputs": {
                "positive": ["text", 0], "negative": ["zero", 0],
                "latent_image": ["latent", 0], "seed": 1,
            }},
        }
        mapping = app.detect_node_mapping(workflow)
        self.assertEqual(mapping["positive_prompt"]["node"], "text")
        self.assertIsNone(mapping["negative_prompt"])
        app.patch_workflow(workflow, mapping, "new positive", "ignored negative", 99)
        self.assertEqual(workflow["text"]["inputs"]["text"], "new positive")
        self.assertEqual(workflow["sampler"]["inputs"]["seed"], 99)

    def test_workflow_without_negative_conditioning_is_valid_and_positive_only(self):
        workflow = {
            "positive": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "prompt", "clip": ["clip", 0]},
            },
            "orphan_negative": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "unused", "clip": ["clip", 0]},
            },
            "latent": {"class_type": "EmptyLatentImage", "inputs": {}},
            "sampler": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "positive": ["positive", 0],
                "latent_image": ["latent", 0],
                "noise_seed": 7,
            }},
        }
        mapping = app.detect_node_mapping(workflow)
        self.assertEqual(mapping["positive_prompt"]["node"], "positive")
        self.assertIsNone(mapping["negative_prompt"])
        app.patch_workflow(workflow, mapping, "authoritative", "auxiliary", 88)
        self.assertEqual(workflow["positive"]["inputs"]["text"], "authoritative")
        self.assertEqual(workflow["orphan_negative"]["inputs"]["text"], "unused")
        self.assertEqual(workflow["sampler"]["inputs"]["noise_seed"], 88)

    def test_preview_scales_only_fast_workflow_to_configured_max_edge(self):
        workflow = {
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "prompt"}},
            "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
                "width": 1152, "height": 896, "batch_size": 1,
            }},
            "sampler": {"class_type": "KSampler", "inputs": {
                "positive": ["positive", 0], "latent_image": ["latent", 0], "seed": 1,
            }},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0]}},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0]}},
        }
        mapping = app.detect_node_mapping(workflow, include_fast=True)
        prepared = app.prepare_fast_workflow(app.copy.deepcopy(workflow), mapping)
        self.assertEqual(
            (prepared["latent"]["inputs"]["width"], prepared["latent"]["inputs"]["height"]),
            (512, 384),
        )
        self.assertEqual(
            (workflow["latent"]["inputs"]["width"], workflow["latent"]["inputs"]["height"]),
            (1152, 896),
        )
        self.assertEqual(app.preview_dimensions(896, 1152, 512), (384, 512))

    def test_preview_retains_lora_model_and_clip_design(self):
        workflow = {
            "checkpoint": {"class_type": "CheckpointLoaderSimple", "inputs": {
                "ckpt_name": "base.safetensors",
            }},
            "lora": {"class_type": "LoraLoader", "inputs": {
                "model": ["checkpoint", 0], "clip": ["checkpoint", 1],
                "lora_name": "art-direction.safetensors",
                "strength_model": 0.8, "strength_clip": 0.6,
            }},
            "positive": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "prompt", "clip": ["lora", 1],
            }},
            "latent": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 1024, "height": 1024,
            }},
            "sampler": {"class_type": "KSampler", "inputs": {
                "model": ["lora", 0], "positive": ["positive", 0],
                "latent_image": ["latent", 0], "seed": 1,
            }},
            "decode": {"class_type": "VAEDecode", "inputs": {
                "samples": ["sampler", 0], "vae": ["checkpoint", 2],
            }},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0]}},
            "detailer": {"class_type": "FaceDetailer", "inputs": {
                "model": ["lora", 0], "image": ["decode", 0], "seed": 2,
            }},
        }
        mapping = app.detect_node_mapping(workflow, include_fast=True)
        prepared = app.prepare_fast_workflow(app.copy.deepcopy(workflow), mapping)
        self.assertEqual(prepared["sampler"]["inputs"]["model"], ["lora", 0])
        self.assertEqual(prepared["positive"]["inputs"]["clip"], ["lora", 1])
        self.assertEqual(prepared["lora"]["inputs"]["strength_model"], 0.8)
        self.assertEqual(prepared["lora"]["inputs"]["strength_clip"], 0.6)
        self.assertNotIn("detailer", prepared)

    def test_fast_profile_requires_scalar_latent_dimensions(self):
        workflow = {
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "prompt"}},
            "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
                "width": ["size", 0], "height": ["size", 1],
            }},
            "sampler": {"class_type": "KSampler", "inputs": {
                "positive": ["positive", 0], "latent_image": ["latent", 0], "seed": 1,
            }},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0]}},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0]}},
        }
        with self.assertRaisesRegex(app.AppError, "must expose scalar width and height"):
            app.detect_node_mapping(workflow, include_fast=True)

    def test_database_declares_negative_conditioning_auxiliary(self):
        database, _ = app.load_database()
        self.assertEqual(database["prompt_defaults"]["conditioning_policy"], {
            "structural_source": "positive",
            "negative_role": "auxiliary_optional",
        })
        root = Path(app.__file__).parent
        html = (root / "client" / "client.html").read_text(encoding="utf-8")
        javascript = (root / "client" / "client.js").read_text(encoding="utf-8")
        self.assertIn("Auxiliary negative", html)
        self.assertIn("Positive-only workflow", javascript)

    def test_model_name_becomes_a_readable_safe_profile_filename(self):
        workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "models/Lumina_2-F16.safetensors"}},
        }
        name = app.workflow_model_name(workflow)
        self.assertEqual(name, "Lumina 2-F16")
        self.assertEqual(app.workflow_profile_slug(name), "lumina-2-f16")

    def test_profile_selection_rename_and_delete_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = {"settings": {}}
            config = root / "config.json"
            config.write_text(app.json.dumps({
                "server": {"host": "127.0.0.1", "port": 8765},
                "comfy": {
                    "url": "http://127.0.0.1:8188",
                    "workflows_dir": "./profiles",
                    "http_timeout_seconds": 15,
                    "status_timeout_seconds": 2,
                    "status_refresh_seconds": 10,
                    "poll_interval_seconds": 1,
                    "generation_timeout_seconds": 600,
                    "preview_max_edge": 512,
                    "profiles": {"production": None, "preview": None},
                },
                "storage": {
                    "output_dir": "./outputs", "proofs_dir": [],
                    "output_format": "png", "jpeg_quality": 95, "strip_exif": True,
                },
                "gallery": {"thumbnail_cache_mb": 512, "thumbnail_max_edge": 512},
                "interface": {"privacy": {"auto_cover_minutes": [5, 15]}},
                "limits": {
                    "max_scene_attempts": 100, "max_storyboards": 20,
                    "max_jobs": 40, "max_previews": 8,
                },
            }), encoding="utf-8")
            directory = root / "profiles"
            directory.mkdir()
            (directory / "model-a.workflow.json").write_text("{}\n", encoding="utf-8")
            (directory / "model-b.workflow.json").write_text("{}\n", encoding="utf-8")
            with (
                patch.object(app, "config_path", return_value=config),
                patch.object(app, "detect_node_mapping", return_value={}),
            ):
                selected = app.select_workflow_profiles(db, root / "database.json", "model-a", "model-b")
                self.assertEqual(selected["production"], "model-a")
                self.assertEqual(selected["preview"], "model-b")
                live = app.select_workflow_profiles(
                    db, root / "database.json", "", "", "live"
                )
                self.assertEqual(live["source"], "live")
                self.assertEqual(live["production"], "model-a")
                self.assertEqual(live["preview"], "model-b")
                renamed = app.rename_workflow_profile(db, root / "database.json", "model-a", "Editorial Model")
                self.assertEqual(renamed["production"], "editorial-model")
                self.assertTrue((directory / "editorial-model.workflow.json").is_file())
                with self.assertRaisesRegex(app.AppError, "Select another"):
                    app.delete_workflow_profile(db, root / "database.json", "model-b")


if __name__ == "__main__":
    unittest.main()
