#!/usr/bin/env python3
"""Small, single-file rule-based prompt composer for a local ComfyUI server."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import re
import secrets
import sys
import subprocess
import time
import uuid
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:  # dry-run deliberately works without the HTTP dependency
    requests = None  # type: ignore[assignment]


class AppError(RuntimeError):
    """An expected, user-facing application error."""


APP_VERSION = "1.6.0"
MEDIA_TYPES = {"image", "video"}
UI_SEED_MIN = 100_000_000_000_000
UI_SEED_MAX = 999_999_999_999_999
UI_SEED_SPAN = UI_SEED_MAX - UI_SEED_MIN + 1


def automatic_ui_seed() -> int:
    """Return a fixed-width decimal seed that remains exact in JavaScript."""
    seed = UI_SEED_MIN + secrets.randbelow(UI_SEED_SPAN)
    return seed if seed % 10 else seed + 1


def deterministic_ui_seed(material: bytes) -> int:
    """Derive a fixed-width UI-safe seed without losing reproducibility."""
    digest = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    seed = UI_SEED_MIN + digest % UI_SEED_SPAN
    return seed if seed % 10 else seed + 1


def validate_media_type(media_type: str) -> str:
    media_type = str(media_type)
    if media_type not in MEDIA_TYPES:
        raise AppError("Media type must be image or video")
    return media_type


def weighted_choice(rng: random.Random, items: list[dict[str, Any]]) -> dict[str, Any]:
    items = [item for item in items if not item.get("disabled", False)]
    if not items:
        raise AppError("No compatible choices remain for a required selection")
    weights = [float(item.get("weight", 1)) for item in items]
    if any(weight <= 0 for weight in weights):
        raise AppError("Every selectable item weight must be greater than zero")
    return rng.choices(items, weights=weights, k=1)[0]


def recipe_focus_compatible(
    item: dict[str, Any], recipe: dict[str, Any] | None, kind: str
) -> bool:
    """Apply only high-confidence anatomical focus constraints."""
    if not recipe:
        return True
    focus = recipe.get("focus_target")
    signals = tags(item) | set(item.get("requires_tags", []))
    if focus == "focus_breasts":
        return bool(signals & {"breasts", "nipples", "breast_focus"})
    if focus == "focus_intimate":
        return (
            bool(signals & {"genitals", "open_legs", "masturbation_pose", "masturbation_action"})
            and not signals & {"breast_focus", "provocative_rear"}
        )
    if focus == "focus_rear":
        required = "provocative_rear" if kind == "pose" else "provocative_action"
        return required in signals
    return True


def database_path() -> Path:
    return Path(__file__).resolve().with_name("database.json")


def config_path() -> Path:
    return Path(__file__).resolve().with_name("config.json")


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AppError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_database() -> tuple[dict[str, Any], Path]:
    path = database_path()
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
        )
    except FileNotFoundError as exc:
        raise AppError(f"Database not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON in {path}: {exc}") from exc
    validate_database(data)
    return data, path


def load_config() -> tuple[dict[str, Any], Path]:
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
    except FileNotFoundError as exc:
        raise AppError(f"Configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise AppError(f"config.json must contain a JSON object: {path}")
    server = config.get("server")
    if not isinstance(server, dict):
        raise AppError("config.server must be an object")
    if not isinstance(server.get("host"), str) or not server["host"]:
        raise AppError("config.server.host must be a non-empty string")
    listen_port = server.get("port")
    if not isinstance(listen_port, int) or isinstance(listen_port, bool) or not 1 <= listen_port <= 65535:
        raise AppError("config.server.port must be an integer from 1 to 65535")
    comfy = config.get("comfy")
    if not isinstance(comfy, dict):
        raise AppError("config.comfy must be an object")
    for key in ("url", "workflows_dir"):
        if not isinstance(comfy.get(key), str) or not comfy[key]:
            raise AppError(f"config.comfy.{key} must be a non-empty string")
    storage = config.get("storage")
    if not isinstance(storage, dict):
        raise AppError("config.storage must be an object")
    if not isinstance(storage.get("output_dir"), str) or not storage["output_dir"]:
        raise AppError("config.storage.output_dir must be a non-empty string")
    if storage.get("output_format") not in {"png", "jpeg", "jpg"}:
        raise AppError("config.storage.output_format must be 'png', 'jpeg', or 'jpg'")
    jpeg_quality = storage.get("jpeg_quality")
    if not isinstance(jpeg_quality, int) or isinstance(jpeg_quality, bool) or not 1 <= jpeg_quality <= 100:
        raise AppError("config.storage.jpeg_quality must be an integer from 1 to 100")
    if not isinstance(storage.get("strip_exif"), bool):
        raise AppError("config.storage.strip_exif must be true or false")
    prompt_debug_log = storage.get("prompt_debug_log", {})
    if not isinstance(prompt_debug_log, dict):
        raise AppError("config.storage.prompt_debug_log must be an object")
    if prompt_debug_log:
        if set(prompt_debug_log) != {"enabled", "path"}:
            raise AppError(
                "config.storage.prompt_debug_log must contain only enabled and path"
            )
        if not isinstance(prompt_debug_log.get("enabled"), bool):
            raise AppError("config.storage.prompt_debug_log.enabled must be true or false")
        if (
            not isinstance(prompt_debug_log.get("path"), str)
            or not prompt_debug_log["path"]
        ):
            raise AppError("config.storage.prompt_debug_log.path must be a non-empty string")
    proofs_dir = storage.get("proofs_dir")
    if isinstance(proofs_dir, str):
        if not proofs_dir:
            raise AppError("config.storage.proofs_dir string cannot be empty")
    elif not isinstance(proofs_dir, list) or not all(
        isinstance(value, str) and value for value in proofs_dir
    ):
        raise AppError("config.storage.proofs_dir must be a path string or an array of path strings")
    gallery = config.get("gallery")
    if not isinstance(gallery, dict):
        raise AppError("config.gallery must be an object")
    thumbnail_cache_mb = gallery.get("thumbnail_cache_mb")
    if (
        not isinstance(thumbnail_cache_mb, int)
        or isinstance(thumbnail_cache_mb, bool)
        or not 0 <= thumbnail_cache_mb <= 4096
    ):
        raise AppError("config.gallery.thumbnail_cache_mb must be an integer from 0 to 4096")
    thumbnail_max_edge = gallery.get("thumbnail_max_edge")
    if (
        not isinstance(thumbnail_max_edge, int) or isinstance(thumbnail_max_edge, bool)
        or not 64 <= thumbnail_max_edge <= 4096
    ):
        raise AppError("config.gallery.thumbnail_max_edge must be an integer from 64 to 4096")
    interface = config.get("interface")
    if not isinstance(interface, dict):
        raise AppError("config.interface must be an object")
    privacy = interface.get("privacy")
    if not isinstance(privacy, dict):
        raise AppError("config.interface.privacy must be an object")
    auto_cover_minutes = privacy.get("auto_cover_minutes")
    if (
        not isinstance(auto_cover_minutes, list) or len(auto_cover_minutes) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1440
            for value in auto_cover_minutes
        )
        or auto_cover_minutes[0] >= auto_cover_minutes[1]
    ):
        raise AppError(
            "config.interface.privacy.auto_cover_minutes must contain two increasing "
            "integer minute values from 1 to 1440"
        )
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise AppError("config.limits must be an object")
    limit_ranges = {
        "max_scene_attempts": (1, 100_000),
        "max_storyboards": (1, 10_000),
        "max_jobs": (1, 10_000),
        "max_previews": (1, 1_000),
    }
    for key, (minimum, maximum) in limit_ranges.items():
        value = limits.get(key)
        if (
            not isinstance(value, int) or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise AppError(f"config.limits.{key} must be an integer from {minimum} to {maximum}")
    number_ranges = {
        "http_timeout_seconds": (0.1, 3600),
        "status_timeout_seconds": (0.1, 300),
        "status_refresh_seconds": (1, 3600),
        "poll_interval_seconds": (0.05, 60),
        "generation_timeout_seconds": (1, 86_400),
    }
    for key, (minimum, maximum) in number_ranges.items():
        value = comfy.get(key)
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise AppError(f"config.comfy.{key} must be a number from {minimum} to {maximum}")
    preview_max_edge = comfy.get("preview_max_edge")
    if (
        not isinstance(preview_max_edge, int)
        or isinstance(preview_max_edge, bool)
        or not 256 <= preview_max_edge <= 2048
        or preview_max_edge % 64
    ):
        raise AppError(
            "config.comfy.preview_max_edge must be a multiple of 64 from 256 to 2048"
        )
    if comfy.get("workflow_source", "profiles") not in {"profiles", "live"}:
        raise AppError("config.comfy.workflow_source must be 'profiles' or 'live'")
    profiles = comfy.get("profiles")
    if not isinstance(profiles, dict):
        raise AppError("config.comfy.profiles must be an object")
    for mode in ("production", "preview"):
        if mode not in profiles:
            raise AppError(f"config.comfy.profiles must contain '{mode}'")
        if profiles.get(mode) is not None and not isinstance(profiles.get(mode), str):
            raise AppError(f"config.comfy.profiles.{mode} must be a string or null")
    media_profiles = comfy.get("media_profiles")
    if media_profiles is not None:
        if not isinstance(media_profiles, dict):
            raise AppError("config.comfy.media_profiles must be an object")
        for media_type in ("image", "video"):
            settings = media_profiles.get(media_type)
            if not isinstance(settings, dict):
                raise AppError(f"config.comfy.media_profiles.{media_type} must be an object")
            if settings.get("source", "profiles") not in {"profiles", "live"}:
                raise AppError(f"config.comfy.media_profiles.{media_type}.source must be profiles or live")
            required_modes = ("production", "preview") if media_type == "image" else ("production",)
            for mode in required_modes:
                if mode not in settings:
                    raise AppError(f"config.comfy.media_profiles.{media_type} must contain '{mode}'")
                if settings.get(mode) is not None and not isinstance(settings.get(mode), str):
                    raise AppError(f"config.comfy.media_profiles.{media_type}.{mode} must be a string or null")
    return config, path


def save_config(config: dict[str, Any]) -> None:
    _, path = load_config()
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def iter_content_items(db: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for values in db.get("human_model_parts", {}).values():
        if isinstance(values, list):
            yield from values
    yield from db.get("colors", [])
    yield from db.get("patterns", [])
    yield from db.get("fabric_textures", [])
    for values in db.get("garments", {}).values():
        yield from values
    for section in (
        "outfit_templates", "interiors", "furniture", "poses", "actions",
        "props", "expressions", "moods", "photography_styles", "shot_sizes",
        "camera_angles", "framings", "focus_targets", "editorial_roles",
        "explicit_recipes", "intimate_arousal_modifiers", "location_zones",
    ):
        yield from db.get(section, [])


def validate_item(item: Any, context: str) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise AppError(f"{context}: every item needs a string id")
    if context != "outfit_templates" and not isinstance(item.get("prompt"), str):
        raise AppError(f"{context}.{item['id']}: missing string prompt")
    weight = item.get("weight", 1)
    if not isinstance(weight, (int, float)) or weight <= 0:
        raise AppError(f"{context}.{item['id']}: weight must be greater than zero")
    if "disabled" in item and not isinstance(item["disabled"], bool):
        raise AppError(f"{context}.{item['id']}: disabled must be true or false")
    if "menu_label" in item and (
        not isinstance(item["menu_label"], str) or not item["menu_label"].strip()
    ):
        raise AppError(f"{context}.{item['id']}: menu_label must be a non-empty string")
    if "menu_group" in item and (
        not isinstance(item["menu_group"], str) or not item["menu_group"].strip()
    ):
        raise AppError(f"{context}.{item['id']}: menu_group must be a non-empty string")
    if "reveals_cameltoe" in item and not isinstance(item["reveals_cameltoe"], bool):
        raise AppError(f"{context}.{item['id']}: reveals_cameltoe must be true or false")
    hands_required = item.get("hands_required", 0)
    if not isinstance(hands_required, int) or not 0 <= hands_required <= 2:
        raise AppError(f"{context}.{item['id']}: hands_required must be an integer from 0 to 2")
    for field in ("requires_environment_tags", "excludes_environment_tags"):
        if field in item and (
            not isinstance(item[field], list)
            or not all(isinstance(tag, str) and tag for tag in item[field])
        ):
            raise AppError(f"{context}.{item['id']}: {field} must be a list of tags")


def validate_database(db: dict[str, Any]) -> None:
    required_sections = (
        "settings", "prompt_defaults", "colors", "patterns", "fabric_textures",
        "human_model_parts", "garments",
        "outfit_templates", "location_zones", "interiors", "furniture", "poses", "actions", "props",
        "expressions", "moods", "photography_styles", "shot_sizes",
        "camera_angles", "framings", "focus_targets", "editorial_roles",
        "explicit_recipes", "intimate_arousal_modifiers",
    )
    for section in required_sections:
        if section not in db:
            raise AppError(f"database.json is missing the '{section}' section")
    settings = db["settings"]
    panties_reveal = settings.get("dressed_panties_reveal")
    if not isinstance(panties_reveal, dict):
        raise AppError("settings.dressed_panties_reveal must be an object")
    reveal_chance = panties_reveal.get("chance")
    if not isinstance(reveal_chance, (int, float)) or not 0 <= reveal_chance <= 1:
        raise AppError("settings.dressed_panties_reveal.chance must be between zero and one")
    if not isinstance(panties_reveal.get("positive_prompt"), str) or not panties_reveal["positive_prompt"].strip():
        raise AppError("settings.dressed_panties_reveal.positive_prompt must be text")
    positive_prefix = db["prompt_defaults"].get("positive_prefix")
    if not isinstance(positive_prefix, str) or positive_prefix.count("{age}") != 1:
        raise AppError(
            "prompt_defaults.positive_prefix must contain exactly one {age} placeholder"
        )
    cameltoe_prompt = db["prompt_defaults"].get("cameltoe_prompt")
    if not isinstance(cameltoe_prompt, str) or not cameltoe_prompt.strip():
        raise AppError("prompt_defaults.cameltoe_prompt must be a non-empty string")
    expected_priority = [
        "subject", "camera_direction", "anatomy", "traits_garments",
        "location_treatment",
    ]
    if db["prompt_defaults"].get("prompt_priority") != expected_priority:
        raise AppError(
            "prompt_defaults.prompt_priority must define subject, camera_direction, "
            "anatomy, traits_garments, location_treatment in that order"
        )
    if db["prompt_defaults"].get("conditioning_policy") != {
        "structural_source": "positive",
        "negative_role": "auxiliary_optional",
    }:
        raise AppError(
            "prompt_defaults.conditioning_policy must keep structural guarantees "
            "in positive and treat negative conditioning as auxiliary_optional"
        )
    negative_profiles = db["prompt_defaults"].get("negative_profiles")
    if not isinstance(negative_profiles, dict) or not all(
        isinstance(negative_profiles.get(key), str)
        for key in ("covered_opaque", "layered_hosiery", "explicit")
    ):
        raise AppError("prompt_defaults.negative_profiles is incomplete")
    progression = settings.get("photoshoot_progression", {})
    nsfw_percent = progression.get("nsfw_final_percent", 50)
    if not isinstance(nsfw_percent, (int, float)) or not 0 <= nsfw_percent <= 100:
        raise AppError("settings.photoshoot_progression.nsfw_final_percent must be between 0 and 100")
    plateau_percent = progression.get("explicit_plateau_percent", 30)
    if not isinstance(plateau_percent, (int, float)) or not 0 <= plateau_percent <= nsfw_percent:
        raise AppError(
            "settings.photoshoot_progression.explicit_plateau_percent must be between 0 "
            "and nsfw_final_percent"
        )
    garment_modifiers = settings.get("garment_modifiers", {})
    if not isinstance(garment_modifiers, dict):
        raise AppError("settings.garment_modifiers must be an object")
    for field in ("pattern_chance", "texture_chance"):
        value = garment_modifiers.get(field, 0)
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise AppError(f"settings.garment_modifiers.{field} must be between 0 and 1")
    lora_rules = settings.get("workflow_lora_rules", [])
    if not isinstance(lora_rules, list):
        raise AppError("settings.workflow_lora_rules must be an array")
    lora_rule_ids: set[str] = set()
    matcher_fields = {
        "stage_levels", "visual_categories",
        "visible_slots_any", "visible_slots_all", "visible_slots_none",
        "body_visibility_any", "body_visibility_all", "body_visibility_none",
        "visible_garment_tags_any", "visible_garment_tags_all",
        "visible_garment_tags_none",
    }
    for rule in lora_rules:
        if not isinstance(rule, dict):
            raise AppError("Every workflow LoRA rule must be an object")
        allowed_rule_fields = {
            "id", "lora_name", "when",
            "strength_model", "strength_clip",
        }
        if not set(rule) <= allowed_rule_fields:
            raise AppError("Workflow LoRA rules contain unsupported fields")
        rule_id = rule.get("id")
        lora_name = rule.get("lora_name")
        when = rule.get("when")
        if not isinstance(rule_id, str) or not rule_id or rule_id in lora_rule_ids:
            raise AppError("Workflow LoRA rule IDs must be unique non-empty strings")
        lora_rule_ids.add(rule_id)
        if not isinstance(lora_name, str) or not lora_name:
            raise AppError(f"Workflow LoRA rule {rule_id} lora_name must be text")
        if not isinstance(when, dict) or not when or not set(when) <= matcher_fields:
            raise AppError(
                f"Workflow LoRA rule {rule_id} when must use supported match fields"
            )
        for field, values in when.items():
            if (
                not isinstance(values, list) or not values
                or not all(isinstance(value, str) and value for value in values)
                or len(values) != len(set(values))
            ):
                raise AppError(
                    f"Workflow LoRA rule {rule_id} when.{field} must be a unique "
                    "non-empty string array"
                )
        for base in ("visible_slots", "body_visibility", "visible_garment_tags"):
            required = set(when.get(f"{base}_all", []))
            forbidden = set(when.get(f"{base}_none", []))
            if required & forbidden:
                raise AppError(
                    f"Workflow LoRA rule {rule_id} has contradictory {base} predicates"
                )
        strengths = [field for field in ("strength_model", "strength_clip") if field in rule]
        if not strengths:
            raise AppError(f"Workflow LoRA rule {rule_id} must set at least one strength")
        for field in strengths:
            value = rule[field]
            if (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not -10 <= value <= 10
            ):
                raise AppError(
                    f"Workflow LoRA rule {rule_id} {field} must be a number from -10 to 10"
                )
    surface_modifiers = settings.get("surface_modifiers", {})
    if not isinstance(surface_modifiers, dict):
        raise AppError("settings.surface_modifiers must be an object")
    for field in ("color_chance", "texture_chance"):
        value = surface_modifiers.get(field, 0)
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise AppError(f"settings.surface_modifiers.{field} must be between 0 and 1")

    ids: set[str] = set()
    index: dict[str, dict[str, Any]] = {}
    for section, values in db["human_model_parts"].items():
        if not isinstance(values, list) or not values:
            raise AppError(f"human_model_parts.{section} must be a non-empty list")
        for item in values:
            validate_item(item, f"human_model_parts.{section}")
            if section in {"breast_size", "breast_shape"} and (
                not isinstance(item.get("covered_prompt"), str)
                or not item["covered_prompt"].strip()
            ):
                raise AppError(
                    f"human_model_parts.{section}.{item['id']}.covered_prompt "
                    "must be non-empty text"
                )
            if section == "skin_marking" and any(
                not isinstance(item.get(field), str)
                for field in ("bra_line_prompt", "panty_line_prompt")
            ):
                raise AppError(
                    f"human_model_parts.skin_marking.{item['id']} must declare "
                    "text bra_line_prompt and panty_line_prompt fields"
                )
            if section == "skin_marking":
                for field in ("bra_line_prompt_by_skin", "panty_line_prompt_by_skin"):
                    variants = item.get(field)
                    if variants is None:
                        continue
                    if (
                        not isinstance(variants, dict)
                        or set(variants) != {"light_skin", "medium_skin", "dark_skin"}
                        or any(
                            not isinstance(value, str) or not value.strip()
                            for value in variants.values()
                        )
                    ):
                        raise AppError(
                            f"human_model_parts.skin_marking.{item['id']}.{field} "
                            "must define non-empty light_skin, medium_skin and dark_skin text"
                        )
            if item["id"] in ids:
                raise AppError(f"Duplicate id: {item['id']}")
            ids.add(item["id"]); index[item["id"]] = item
    for section, values in db["garments"].items():
        if not isinstance(values, list) or not values:
            raise AppError(f"garments.{section} must be a non-empty list")
        for item in values:
            validate_item(item, f"garments.{section}")
            if item["id"] in ids:
                raise AppError(f"Duplicate id: {item['id']}")
            ids.add(item["id"]); index[item["id"]] = item
    for section in (
        "colors", "patterns", "fabric_textures", "outfit_templates", "location_zones", "interiors", "furniture", "poses", "actions",
        "props", "expressions", "moods", "photography_styles", "shot_sizes",
        "camera_angles", "framings", "focus_targets", "editorial_roles",
        "explicit_recipes", "intimate_arousal_modifiers",
    ):
        values = db[section]
        if not isinstance(values, list) or not values:
            raise AppError(f"{section} must be a non-empty list")
        for item in values:
            validate_item(item, section)
            if item["id"] in ids:
                raise AppError(f"Duplicate id: {item['id']}")
            ids.add(item["id"]); index[item["id"]] = item

    enabled_ids = {item_id for item_id, item in index.items() if not item.get("disabled", False)}
    for zone in db["location_zones"]:
        for field in ("match_any_tags", "capabilities", "environment_tags"):
            if not isinstance(zone.get(field), list) or not all(
                isinstance(value, str) and value for value in zone[field]
            ):
                raise AppError(
                    f"location_zones.{zone['id']}.{field} must be a list of strings"
                )
    for recipe in db["explicit_recipes"]:
        for field, section in (
            ("shot_size", "shot_sizes"),
            ("camera_angle", "camera_angles"),
            ("focus_target", "focus_targets"),
        ):
            section_ids = {item["id"] for item in db[section]}
            if recipe.get(field) not in section_ids:
                raise AppError(
                    f"explicit_recipes.{recipe['id']}.{field} references unknown id"
                )
            options = recipe.get(f"{field}_options")
            if options is not None and (
                not isinstance(options, list) or not options
                or len(options) != len(set(options))
                or not all(isinstance(item_id, str) for item_id in options)
                or not set(options).issubset(section_ids)
            ):
                raise AppError(
                    f"explicit_recipes.{recipe['id']}.{field}_options must "
                    f"reference unique existing {section}"
                )
    human_defaults = settings.get("human_defaults", {})
    if not isinstance(human_defaults, dict):
        raise AppError("settings.human_defaults must be an object")
    default_pools = human_defaults.get("pools", {})
    if not isinstance(default_pools, dict):
        raise AppError("settings.human_defaults.pools must be an object")
    for category, item_ids in default_pools.items():
        if category not in db["human_model_parts"]:
            raise AppError(f"settings.human_defaults.pools has unknown category: {category}")
        if not isinstance(item_ids, list) or not all(
            isinstance(item_id, str) and item_id for item_id in item_ids
        ):
            raise AppError(
                f"settings.human_defaults.pools.{category} must be a list of IDs"
            )
        if len(item_ids) != len(set(item_ids)):
            raise AppError(
                f"settings.human_defaults.pools.{category} contains duplicate IDs"
            )
        category_ids = {
            item["id"] for item in db["human_model_parts"][category]
            if not item.get("disabled", False)
        }
        unknown_ids = set(item_ids) - category_ids
        if unknown_ids:
            raise AppError(
                f"settings.human_defaults.pools.{category} references unavailable items: "
                f"{sorted(unknown_ids)}"
            )
    color_ids = {item["id"] for item in db["colors"]}
    enabled_color_ids = {
        item["id"] for item in db["colors"] if not item.get("disabled", False)
    }
    if not enabled_color_ids:
        raise AppError("colors must contain at least one enabled item")
    wardrobe_compatibility = settings.get("wardrobe_compatibility")
    if not isinstance(wardrobe_compatibility, dict):
        raise AppError("settings.wardrobe_compatibility must be an object")
    color_families = wardrobe_compatibility.get("color_families")
    if not isinstance(color_families, dict) or not color_families:
        raise AppError("settings.wardrobe_compatibility.color_families must be an object")
    family_colors: list[str] = []
    for family, members in color_families.items():
        if (
            not isinstance(family, str) or not family
            or not isinstance(members, list) or not members
            or not all(isinstance(color_id, str) for color_id in members)
            or len(members) != len(set(members))
        ):
            raise AppError(
                f"settings.wardrobe_compatibility.color_families.{family} "
                "must be a non-empty unique list"
            )
        family_colors.extend(members)
    if set(family_colors) != color_ids or len(family_colors) != len(set(family_colors)):
        raise AppError(
            "settings.wardrobe_compatibility.color_families must assign every color "
            "to exactly one family"
        )
    known_slots = {
        slot for template in db["outfit_templates"] for slot in template["slots"]
    }
    known_garment_tags = set().union(*(
        tags(item) for values in db["garments"].values() for item in values
    ))
    for section in ("visible_layer_rules", "optional_inner_layers"):
        rules = wardrobe_compatibility.get(section)
        if not isinstance(rules, list):
            raise AppError(f"settings.wardrobe_compatibility.{section} must be a list")
        for rule in rules:
            context = f"settings.wardrobe_compatibility.{section}"
            if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
                raise AppError(f"Every {context} entry needs a string id")
            outer_slots = rule.get("outer_slots")
            if (
                not isinstance(outer_slots, list) or not outer_slots
                or len(outer_slots) != len(set(outer_slots))
                or not set(outer_slots).issubset(known_slots)
            ):
                raise AppError(f"{context}.{rule['id']}.outer_slots contains invalid slots")
            if rule.get("inner_slot") not in known_slots:
                raise AppError(f"{context}.{rule['id']}.inner_slot is invalid")
            required_tags = rule.get("outer_tags_any")
            if (
                not isinstance(required_tags, list) or not required_tags
                or len(required_tags) != len(set(required_tags))
                or not set(required_tags).issubset(known_garment_tags)
            ):
                raise AppError(f"{context}.{rule['id']}.outer_tags_any contains invalid tags")
            if section == "visible_layer_rules":
                if rule.get("color_relation") != "same_family":
                    raise AppError(f"{context}.{rule['id']}.color_relation is unsupported")
                if not isinstance(rule.get("suppress_inner_pattern"), bool):
                    raise AppError(f"{context}.{rule['id']}.suppress_inner_pattern must be boolean")
            else:
                chance = rule.get("chance")
                if not isinstance(chance, (int, float)) or not 0 <= chance <= 1:
                    raise AppError(f"{context}.{rule['id']}.chance must be between zero and one")
                levels = rule.get("drop_uncovered_stage_levels")
                if (
                    not isinstance(levels, list) or not levels
                    or not set(levels).issubset({"covered", "lingerie", *NSFW_LEVELS})
                ):
                    raise AppError(f"{context}.{rule['id']}.drop_uncovered_stage_levels is invalid")
                coverage_slots = rule.get("coverage_slots")
                if (
                    not isinstance(coverage_slots, list) or not coverage_slots
                    or not set(coverage_slots).issubset(known_slots)
                ):
                    raise AppError(f"{context}.{rule['id']}.coverage_slots is invalid")
    visibility_rules = wardrobe_compatibility.get("stage_visibility_rules")
    if not isinstance(visibility_rules, list):
        raise AppError("settings.wardrobe_compatibility.stage_visibility_rules must be a list")
    for rule in visibility_rules:
        context = "settings.wardrobe_compatibility.stage_visibility_rules"
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise AppError(f"Every {context} entry needs a string id")
        for field in ("outer_slots", "hide_slots"):
            values = rule.get(field)
            if (
                not isinstance(values, list) or not values
                or len(values) != len(set(values))
                or not set(values).issubset(known_slots)
            ):
                raise AppError(f"{context}.{rule['id']}.{field} contains invalid slots")
        outer_tags = rule.get("outer_tags_any")
        if (
            not isinstance(outer_tags, list) or not outer_tags
            or not set(outer_tags).issubset(known_garment_tags)
        ):
            raise AppError(f"{context}.{rule['id']}.outer_tags_any contains invalid tags")
        visibility = rule.get("remove_body_visibility")
        if (
            not isinstance(visibility, list) or not visibility
            or not set(visibility).issubset(SFW_BLOCKED_VISIBILITY)
        ):
            raise AppError(f"{context}.{rule['id']}.remove_body_visibility is invalid")
    render_contracts = wardrobe_compatibility.get("render_contracts")
    if not isinstance(render_contracts, list):
        raise AppError("settings.wardrobe_compatibility.render_contracts must be a list")
    for rule in render_contracts:
        context = "settings.wardrobe_compatibility.render_contracts"
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise AppError(f"Every {context} entry needs a string id")
        if rule.get("slot") not in known_slots:
            raise AppError(f"{context}.{rule['id']}.slot is invalid")
        garment_tags = rule.get("garment_tags_any")
        if (
            not isinstance(garment_tags, list) or not garment_tags
            or not set(garment_tags).issubset(known_garment_tags)
        ):
            raise AppError(f"{context}.{rule['id']}.garment_tags_any contains invalid tags")
        visibility = rule.get("body_visibility_any")
        if (
            not isinstance(visibility, list) or not visibility
            or not set(visibility).issubset(SFW_BLOCKED_VISIBILITY)
        ):
            raise AppError(f"{context}.{rule['id']}.body_visibility_any is invalid")
        if not isinstance(rule.get("positive_prompt"), str) or not rule["positive_prompt"].strip():
            raise AppError(f"{context}.{rule['id']}.positive_prompt must be text")
    surface_pool_sections = {
        "colors": {item["id"] for item in db["colors"] if not item.get("disabled", False)},
        "textures": {
            item["id"] for item in db["fabric_textures"]
            if not item.get("disabled", False)
        },
    }
    for field, available in surface_pool_sections.items():
        values = surface_modifiers.get(field)
        if (
            not isinstance(values, list) or not values
            or not all(isinstance(value, str) for value in values)
            or len(values) != len(set(values))
            or not set(values).issubset(available)
        ):
            raise AppError(
                f"settings.surface_modifiers.{field} must be a non-empty unique "
                "list of enabled modifier IDs"
            )
    for furniture in db["furniture"]:
        for field in ("surface_color_target", "surface_texture_target"):
            if field in furniture and (
                not isinstance(furniture[field], str) or not furniture[field].strip()
            ):
                raise AppError(f"furniture.{furniture['id']}.{field} must be non-empty text")
    for section, values in db["human_model_parts"].items():
        if not any(not item.get("disabled", False) for item in values):
            raise AppError(f"human_model_parts.{section} must contain at least one enabled item")
    for section, values in db["garments"].items():
        if not any(not item.get("disabled", False) for item in values):
            raise AppError(f"garments.{section} must contain at least one enabled item")
    for section in (
        "outfit_templates", "interiors", "furniture", "poses", "actions",
        "expressions", "moods", "photography_styles",
    ):
        if not any(not item.get("disabled", False) for item in db[section]):
            raise AppError(f"{section} must contain at least one enabled item")
    garment_catalogs = set(db["garments"])
    garment_ids = {
        item["id"] for values in db["garments"].values() for item in values
    }
    hosiery_modes = {"waist_continuous", "self_supporting", "garter_required"}
    for item in db["garments"].get("legwear", []):
        wording = f"{item['id']} {item.get('prompt', '')}".casefold()
        is_hosiery = bool(tags(item) & {"pantyhose", "stockings"}) or any(
            term in wording for term in ("pantyhose", "tights", "stockings")
        )
        mode = item.get("support_mode")
        if is_hosiery and mode not in hosiery_modes:
            raise AppError(
                f"Legwear {item['id']} must declare a supported support_mode"
            )
        if not is_hosiery and mode is not None:
            raise AppError(
                f"Non-hosiery legwear {item['id']} cannot declare support_mode"
            )
    supported_state_sequences = {
        ("worn_closed", "unbuttoned_open", "removed"),
        ("worn_closed", "lowered_to_hips", "removed"),
    }
    for values in db["garments"].values():
        for item in values:
            states = item.get("supported_states")
            if states is not None and tuple(states) not in supported_state_sequences:
                raise AppError(
                    f"Garment {item['id']} has an unsupported ordered state sequence"
                )
    reveal_outer_slots = panties_reveal.get("outer_slots")
    reveal_outer_ids = panties_reveal.get("compatible_outer_ids")
    if (
        not isinstance(reveal_outer_slots, list) or not reveal_outer_slots
        or len(reveal_outer_slots) != len(set(reveal_outer_slots))
        or not set(reveal_outer_slots).issubset({"lowerwear", "full_body"})
    ):
        raise AppError("settings.dressed_panties_reveal.outer_slots is invalid")
    if (
        not isinstance(reveal_outer_ids, list) or not reveal_outer_ids
        or len(reveal_outer_ids) != len(set(reveal_outer_ids))
        or not set(reveal_outer_ids).issubset(garment_ids)
    ):
        raise AppError("settings.dressed_panties_reveal.compatible_outer_ids is invalid")
    template_ids = {item["id"] for item in db["outfit_templates"]}
    layer_rules = settings.get("garment_layer_rules", [])
    if not isinstance(layer_rules, list):
        raise AppError("settings.garment_layer_rules must be a list")
    for rule in layer_rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise AppError("Every garment layer rule needs a string id")
        for field, available in (
            ("template_ids", template_ids),
            ("allowed_outer_ids", garment_ids),
            ("allowed_inner_ids", garment_ids),
        ):
            values = rule.get(field)
            if (
                not isinstance(values, list) or not values
                or not all(isinstance(value, str) and value for value in values)
                or len(values) != len(set(values))
                or not set(values).issubset(available)
            ):
                raise AppError(
                    f"settings.garment_layer_rules.{rule['id']}.{field} must "
                    "reference unique existing IDs"
                )
        for field in ("outer_slot", "inner_slot"):
            if not isinstance(rule.get(field), str) or not rule[field]:
                raise AppError(
                    f"settings.garment_layer_rules.{rule['id']}.{field} must be text"
                )
    for section in ("patterns", "fabric_textures"):
        for item in db[section]:
            allowed = item.get("allowed_garment_ids")
            if (
                not isinstance(allowed, list) or not allowed
                or not all(isinstance(item_id, str) and item_id for item_id in allowed)
                or len(allowed) != len(set(allowed))
            ):
                raise AppError(
                    f"{section}.{item['id']}.allowed_garment_ids must be a non-empty unique list"
                )
            unknown = set(allowed) - garment_ids
            if unknown:
                raise AppError(
                    f"{section}.{item['id']} references unknown garments: {sorted(unknown)}"
                )
    for item in index.values():
        for key in ("requires", "excludes"):
            for reference in item.get(key, []):
                if reference not in ids:
                    raise AppError(f"{item['id']}.{key} references unknown id '{reference}'")
        unknown_colors = set(item.get("allowed_colors", [])) - color_ids
        if unknown_colors:
            raise AppError(f"{item['id']} references unknown colors: {sorted(unknown_colors)}")
        if not item.get("disabled", False):
            disabled_requirements = set(item.get("requires", [])) - enabled_ids
            if disabled_requirements:
                raise AppError(
                    f"Enabled item {item['id']} requires disabled IDs: "
                    f"{sorted(disabled_requirements)}"
                )
            configured_colors = set(item.get("allowed_colors", []))
            if configured_colors and not configured_colors & enabled_color_ids:
                raise AppError(f"Enabled item {item['id']} has no enabled allowed colors")

    prompt_owners: dict[str, str] = {}
    internal_prompt_phrases = {
        "production variation", "editorial variation", "understated variation",
        "realistic variation", "production detail", "construction detail",
    }
    for item in iter_content_items(db):
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        normalized = re.sub(r"\s+", " ", prompt.strip().casefold())
        internal = next(
            (phrase for phrase in internal_prompt_phrases if phrase in normalized), None
        )
        if internal:
            raise AppError(
                f"{item['id']}.prompt contains internal non-visual wording: {internal}"
            )
        if len(prompt.split()) > 48:
            raise AppError(f"{item['id']}.prompt is too long for a catalog fragment")
        owner = prompt_owners.get(normalized)
        if owner is not None:
            raise AppError(
                f"{item['id']}.prompt duplicates the visual wording of {owner}"
            )
        prompt_owners[normalized] = item["id"]

    for template in db["outfit_templates"]:
        if template.get("catalog_category") not in CATALOG_CATEGORIES:
            raise AppError(
                f"Template {template['id']} catalog_category must be normal or luxury"
            )
        slots = template.get("slots")
        stages = template.get("stages")
        if not isinstance(slots, dict) or not slots:
            raise AppError(f"Template {template['id']} needs a non-empty slots object")
        if not isinstance(stages, list) or not stages:
            raise AppError(f"Template {template['id']} needs at least one stage")
        for slot, rule in slots.items():
            catalog = rule.get("catalog")
            if catalog not in garment_catalogs:
                raise AppError(f"Template {template['id']} slot {slot} has unknown catalog")
            chance = rule.get("chance", 1)
            if not isinstance(chance, (int, float)) or not 0 <= chance <= 1:
                raise AppError(f"Template {template['id']} slot {slot} chance must be 0..1")
            for field in ("required_tags", "required_any_tags", "excludes_tags"):
                values = rule.get(field, [])
                if (
                    not isinstance(values, list)
                    or not all(isinstance(value, str) and value for value in values)
                    or len(values) != len(set(values))
                ):
                    raise AppError(
                        f"Template {template['id']} slot {slot} {field} must be a unique list of tags"
                    )
            allowed_ids = rule.get("allowed_ids", [])
            catalog_ids = {item["id"] for item in db["garments"][catalog]}
            if (
                not isinstance(allowed_ids, list)
                or not all(isinstance(item_id, str) and item_id for item_id in allowed_ids)
                or len(allowed_ids) != len(set(allowed_ids))
                or not set(allowed_ids).issubset(catalog_ids)
            ):
                raise AppError(
                    f"Template {template['id']} slot {slot} allowed_ids must reference "
                    f"unique garments from {catalog}"
                )
            candidates = [
                item for item in db["garments"][catalog]
                if not item.get("disabled", False)
                and (not allowed_ids or item["id"] in allowed_ids)
                and set(rule.get("required_tags", [])).issubset(tags(item))
                and (
                    not rule.get("required_any_tags")
                    or set(rule["required_any_tags"]) & tags(item)
                )
                and not set(rule.get("excludes_tags", [])) & tags(item)
            ]
            if not candidates:
                raise AppError(
                    f"Template {template['id']} slot {slot} filters out every enabled garment"
                )
        if {"bra", "panties"}.issubset(slots):
            bra_group = slots["bra"].get("color_group")
            panties_group = slots["panties"].get("color_group")
            if not bra_group or bra_group != panties_group:
                raise AppError(
                    f"Template {template['id']} must coordinate bra and panties "
                    "through one shared color_group"
                )
        for stage in stages:
            if not isinstance(stage.get("id"), str) or not isinstance(stage.get("level"), str):
                raise AppError(f"Template {template['id']} has an invalid stage")
            unknown_slots = set(stage.get("visible_slots", [])) - set(slots)
            if unknown_slots:
                raise AppError(f"Template {template['id']} stage has unknown slots: {sorted(unknown_slots)}")

    categorized = list(db["outfit_templates"]) + list(db["interiors"]) + list(db["furniture"])
    categorized.extend(
        item for values in db["garments"].values() for item in values
    )
    for item in categorized:
        if catalog_category(item) not in CATALOG_CATEGORIES:
            raise AppError(
                f"{item['id']} catalog category must be normal or luxury"
            )

    scene_defaults = settings.get("scene_defaults")
    if not isinstance(scene_defaults, dict):
        raise AppError("settings.scene_defaults must be an object")
    category_specs = {
        "wardrobe_categories": (
            CATALOG_CATEGORIES,
            {
                template["catalog_category"] for template in db["outfit_templates"]
                if not template.get("disabled", False)
            },
        ),
        "environment_categories": (
            CATALOG_CATEGORIES,
            {
                catalog_category(interior)
                for interior in db["interiors"] if not interior.get("disabled", False)
            },
        ),
    }
    for field, (allowed, available) in category_specs.items():
        values = scene_defaults.get(field)
        if (
            not isinstance(values, list) or not values
            or not all(isinstance(value, str) for value in values)
            or len(values) != len(set(values))
            or not set(values).issubset(allowed)
        ):
            raise AppError(
                f"settings.scene_defaults.{field} must be a non-empty unique list "
                f"containing only {sorted(allowed)}"
            )
        if not set(values) & available:
            raise AppError(f"settings.scene_defaults.{field} has no enabled candidates")

    scene_pools = scene_defaults.get("pools", {})
    if not isinstance(scene_pools, dict):
        raise AppError("settings.scene_defaults.pools must be an object")
    scene_pool_sections = {
        "interiors": db["interiors"],
        "furniture": db["furniture"],
        "moods": db["moods"],
        "photography_styles": db["photography_styles"],
        "explicit_photography_styles": db["photography_styles"],
    }
    unknown_scene_pools = set(scene_pools) - set(scene_pool_sections)
    if unknown_scene_pools:
        raise AppError(
            f"settings.scene_defaults.pools has unknown sections: {sorted(unknown_scene_pools)}"
        )
    for section, item_ids in scene_pools.items():
        if not isinstance(item_ids, list) or not all(
            isinstance(item_id, str) and item_id for item_id in item_ids
        ):
            raise AppError(f"settings.scene_defaults.pools.{section} must be a list of IDs")
        if len(item_ids) != len(set(item_ids)):
            raise AppError(f"settings.scene_defaults.pools.{section} contains duplicate IDs")
        enabled_section_ids = {
            item["id"] for item in scene_pool_sections[section]
            if not item.get("disabled", False)
        }
        unavailable = set(item_ids) - enabled_section_ids
        if unavailable:
            raise AppError(
                f"settings.scene_defaults.pools.{section} references unavailable items: "
                f"{sorted(unavailable)}"
            )

def detect_fast_mode_mapping(workflow: dict[str, Any]) -> dict[str, Any]:
    base_candidates = []
    for sampler_id, sampler in workflow.items():
        class_name = str(sampler.get("class_type", "")).lower()
        if "sampler" not in class_name or "detailer" in class_name:
            continue
        latent_link = sampler.get("inputs", {}).get("latent_image")
        if not (isinstance(latent_link, list) and len(latent_link) == 2):
            continue
        latent_node = workflow.get(str(latent_link[0]), {})
        latent_class = str(latent_node.get("class_type", "")).lower()
        if "latent" not in latent_class or "empty" not in latent_class:
            continue
        decoders = [
            node_id for node_id, node in workflow.items()
            if str(node.get("class_type", "")).lower() == "vaedecode"
            and node.get("inputs", {}).get("samples") == [sampler_id, 0]
        ]
        if len(decoders) == 1:
            base_candidates.append((sampler_id, decoders[0], str(latent_link[0])))
    if len(base_candidates) != 1:
        found = ", ".join(candidate[0] for candidate in base_candidates) or "none"
        raise AppError(
            "Fast-test mapping is ambiguous: expected one base sampler fed by an empty "
            f"latent with one VAE Decode, found {found}"
        )
    sampler_id, decode_id, latent_id = base_candidates[0]
    latent_inputs = workflow[latent_id].get("inputs", {})
    width = latent_inputs.get("width")
    height = latent_inputs.get("height")
    if (
        not isinstance(width, int) or isinstance(width, bool) or width <= 0
        or not isinstance(height, int) or isinstance(height, bool) or height <= 0
    ):
        raise AppError(
            f"Fast-test latent {latent_id} must expose scalar width and height inputs"
        )
    output_targets = []
    for node_id, node in workflow.items():
        class_name = str(node.get("class_type", "")).lower()
        if "saveimage" not in class_name and "previewimage" not in class_name:
            continue
        if "images" in node.get("inputs", {}):
            output_targets.append({
                "node": node_id,
                "input": "images",
                "source": [decode_id, 0],
            })
    if not output_targets:
        raise AppError("Fast-test mapping could not find a SaveImage or PreviewImage output")
    return {
        "base_sampler": sampler_id,
        "output_targets": output_targets,
        "dimensions": {
            "node": latent_id,
            "width_input": "width",
            "height_input": "height",
            "width": width,
            "height": height,
        },
    }


def workflow_is_video(workflow: Any) -> bool:
    if not isinstance(workflow, dict):
        return False
    return any(
        any(marker in str(node.get("class_type", "")).casefold() for marker in ("savevideo", "videocombine", "createvideo"))
        for node in workflow.values() if isinstance(node, dict)
    )


def detect_video_node_mapping(workflow: dict[str, Any]) -> dict[str, Any]:
    """Detect the small set of controls needed by an image-to-video workflow."""
    if not isinstance(workflow, dict) or not workflow:
        raise AppError("Video workflow must be a non-empty JSON object")
    image_targets = [
        {"node": node_id, "input": "image"}
        for node_id, node in workflow.items()
        if isinstance(node, dict)
        and str(node.get("class_type", "")).casefold() == "loadimage"
        and isinstance(node.get("inputs"), dict)
        and isinstance(node["inputs"].get("image"), str)
    ]
    if not image_targets:
        raise AppError("Video workflow must expose a LoadImage image input")
    prompt_targets = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        class_name = str(node.get("class_type", "")).casefold()
        if isinstance(inputs.get("prompt"), str):
            prompt_targets.append({"node": node_id, "input": "prompt"})
        elif "primitive" in class_name and isinstance(inputs.get("value"), str):
            prompt_targets.append({"node": node_id, "input": "value"})
    if not prompt_targets:
        raise AppError("Video workflow must expose a text prompt input")
    # Prefer the explicit prompt primitive over optional prompt-enhancement nodes.
    prompt_target = next(
        (target for target in prompt_targets if "primitive" in str(workflow[target["node"]].get("class_type", "")).casefold()),
        prompt_targets[0],
    )
    seed_targets = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        class_name = str(node.get("class_type", "")).casefold()
        if (
            "randomnoise" in class_name
            and isinstance(inputs.get("noise_seed"), int)
            and not isinstance(inputs.get("noise_seed"), bool)
        ):
            seed_targets.append({"node": node_id, "input": "noise_seed"})
        elif (
            "sampler" in class_name
            and isinstance(inputs.get("seed"), int)
            and not isinstance(inputs.get("seed"), bool)
        ):
            seed_targets.append({"node": node_id, "input": "seed"})
    if not seed_targets:
        raise AppError("Video workflow must expose at least one scalar noise seed")
    output_nodes = [
        node_id for node_id, node in workflow.items()
        if isinstance(node, dict)
        and any(marker in str(node.get("class_type", "")).casefold() for marker in ("savevideo", "videocombine", "createvideo"))
    ]
    if not output_nodes:
        raise AppError("Video workflow must expose a SaveVideo or VHS Video Combine output")
    duration = None
    # LTX 2.5's captured graph derives frame count from duration * FPS + 1.
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if "emptyltxvlatentvideo" not in str(node.get("class_type", "")).casefold():
            continue
        node_inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        length_link = node_inputs.get("length")
        expression = workflow.get(str(length_link[0])) if isinstance(length_link, list) and len(length_link) == 2 else None
        if not isinstance(expression, dict) or "mathexpression" not in str(expression.get("class_type", "")).casefold():
            continue
        expression_inputs = expression.get("inputs") if isinstance(expression.get("inputs"), dict) else {}
        source = expression_inputs.get("values.a")
        candidate = workflow.get(str(source[0])) if isinstance(source, list) and len(source) == 2 else None
        candidate_inputs = candidate.get("inputs") if isinstance(candidate, dict) and isinstance(candidate.get("inputs"), dict) else {}
        value = candidate_inputs.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            duration = {"node": str(source[0]), "input": "value"}
            break
    if duration is None:
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            for key in ("duration", "seconds"):
                if isinstance(inputs.get(key), (int, float)) and not isinstance(inputs.get(key), bool):
                    duration = {"node": node_id, "input": key}
                    break
            if duration:
                break
    if duration is None:
        raise AppError("Video workflow must expose a duration input")
    return {
        "media_type": "video",
        "image_targets": image_targets,
        "prompt": prompt_target,
        "inference_seed": seed_targets,
        "duration": duration,
        "output_nodes": output_nodes,
    }


def detect_node_mapping(
    workflow: dict[str, Any], include_fast: bool = False
) -> dict[str, Any]:
    """Discover workflow-specific prompt and seed targets for this process only."""
    def upstream_nodes(source: Any) -> set[str]:
        found: set[str] = set()
        pending = [source]
        while pending:
            link = pending.pop()
            if not (isinstance(link, list) and len(link) == 2):
                continue
            node_id = str(link[0])
            if node_id in found or node_id not in workflow:
                continue
            found.add(node_id)
            pending.extend(workflow[node_id].get("inputs", {}).values())
        return found

    text_candidates = [
        (node_id, node) for node_id, node in workflow.items()
        if isinstance(node.get("inputs", {}).get("text"), str)
    ]
    text_ids = {node_id for node_id, _ in text_candidates}
    positive_ids: set[str] = set()
    negative_ids: set[str] = set()
    for node in workflow.values():
        class_type = str(node.get("class_type", "")).lower()
        if "sampler" not in class_type or "detailer" in class_type:
            continue
        inputs = node.get("inputs", {})
        positive_upstream = upstream_nodes(inputs.get("positive"))
        negative_upstream = upstream_nodes(inputs.get("negative"))
        sampler_negative_zeroed = any(
            "conditioningzeroout" in str(workflow[node_id].get("class_type", "")).lower()
            for node_id in negative_upstream
        )
        positive_ids.update(positive_upstream & text_ids)
        if not sampler_negative_zeroed:
            negative_ids.update(negative_upstream & text_ids)
    if not positive_ids:
        positive_ids = {node_id for node_id, _ in text_candidates if "positive" in node_id.lower()}
    if len(positive_ids) != 1 or len(negative_ids) > 1:
        candidates = ", ".join(
            f"{node_id}:{node.get('class_type')}" for node_id, node in text_candidates
        )
        raise AppError(f"Prompt node detection is ambiguous. Text candidates: {candidates}")
    seed_targets = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", "")).lower()
        if "sampler" not in class_type and "detailer" not in class_type:
            continue
        for input_name in ("seed", "noise_seed"):
            if isinstance(node.get("inputs", {}).get(input_name), int):
                seed_targets.append({"node": node_id, "input": input_name})
    if not seed_targets:
        raise AppError("Could not find a scalar seed input in sampler/detailer nodes")
    lora_targets = []
    for node_id, node in workflow.items():
        inputs = node.get("inputs", {})
        lora_name = inputs.get("lora_name")
        if (
            "lora" not in str(node.get("class_type", "")).casefold()
            or not isinstance(lora_name, str) or not lora_name
        ):
            continue
        strength_inputs = [
            field for field in ("strength_model", "strength_clip")
            if isinstance(inputs.get(field), (int, float))
            and not isinstance(inputs.get(field), bool)
        ]
        lora_targets.append({
            "node": node_id,
            "lora_name": lora_name,
            "strength_inputs": strength_inputs,
        })
    mapping = {
        "positive_prompt": {"node": next(iter(positive_ids)), "input": "text"},
        "negative_prompt": (
            {"node": next(iter(negative_ids)), "input": "text"}
            if negative_ids else None
        ),
        "inference_seed": seed_targets,
        "loras": lora_targets,
    }
    if include_fast:
        mapping["fast_mode"] = detect_fast_mode_mapping(workflow)
    return mapping


def tags(item: dict[str, Any]) -> set[str]:
    return set(item.get("tags", []))


CATALOG_CATEGORIES = {"normal", "luxury"}


def catalog_category(item: dict[str, Any]) -> str:
    """Return one common production tier for wardrobe, sets and surfaces."""
    return item.get("catalog_category", "")


def category_allows(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    """Normal parents are strict; luxury parents may use sensible base pieces."""
    return catalog_category(parent) == "luxury" or catalog_category(child) == "normal"


def apply_preferred_pool(
    candidates: list[dict[str, Any]], preferred_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """Use the curated pool when applicable, otherwise preserve category fallback."""
    preferred = set(preferred_ids)
    pooled = [item for item in candidates if item["id"] in preferred]
    return pooled or candidates


def prefer_catalog_category(
    candidates: list[dict[str, Any]], category: str
) -> list[dict[str, Any]]:
    preferred = [item for item in candidates if catalog_category(item) == category]
    return preferred or candidates


OPAQUE_LINGERIE_BLOCKED_TAGS = {
    "explicit", "sheer", "transparent", "open_cup", "crotchless",
}
CHEST_GARMENT_SLOTS = {"upperwear", "full_body", "outerwear", "bra"}


def color_family(db: dict[str, Any], color_id: str) -> str:
    families = db["settings"]["wardrobe_compatibility"]["color_families"]
    return next(family for family, members in families.items() if color_id in members)


def garment_compatible_with_template_stages(
    template: dict[str, Any], slot: str, garment: dict[str, Any]
) -> bool:
    """Keep anatomy-neutral lingerie stages from selecting revealing underwear."""
    if slot not in {"bra", "panties"}:
        if not (tags(garment) & {"sheer", "transparent"}):
            return True
        for stage in template.get("stages", []):
            visible = set(stage.get("visible_slots", []))
            if (
                stage.get("level") == "covered"
                and slot in visible
                and slot in CHEST_GARMENT_SLOTS
                and "bra" not in template["slots"]
                and not any(
                    other != slot
                    and other in CHEST_GARMENT_SLOTS
                    and template["slots"][other].get("required", False)
                    for other in visible
                )
            ):
                return False
        return True
    anatomy = {"breasts", "nipples"} if slot == "bra" else {"pubic_area", "genitals"}
    requires_opaque = any(
        slot in stage.get("visible_slots", [])
        and not (anatomy & set(stage.get("body_visibility", [])))
        for stage in template.get("stages", [])
    )
    return not requires_opaque or not (tags(garment) & OPAQUE_LINGERIE_BLOCKED_TAGS)


def garment_matches_template_slot(
    db: dict[str, Any], template: dict[str, Any], slot: str,
    garment: dict[str, Any], content_mode: str = "progressive",
) -> bool:
    """Apply the same structural slot contract in Composer and Director."""
    rule = template["slots"][slot]
    garment_tags = tags(garment)
    template_requires_support_belt = any(
        "hosiery_support_belt" in candidate.get("required_tags", [])
        for candidate in template["slots"].values()
    )
    support_mode = garment.get("support_mode")
    if support_mode == "garter_required" and not template_requires_support_belt:
        return False
    if support_mode and support_mode != "garter_required" and template_requires_support_belt:
        return False
    if (
        "hosiery_support_belt" in garment_tags
        and not template_requires_support_belt
    ):
        return False
    visible_in_sfw = any(
        is_sfw_stage(stage) and slot in stage.get("visible_slots", [])
        for stage in effective_photoshoot_stages(template)
    )
    return (
        garment in db["garments"][rule["catalog"]]
        and not garment.get("disabled", False)
        and (not rule.get("allowed_ids") or garment["id"] in rule["allowed_ids"])
        and category_allows(template, garment)
        and garment_allowed_by_layer_rules(db, template["id"], slot, garment["id"])
        and set(rule.get("required_tags", [])).issubset(garment_tags)
        and (
            not rule.get("required_any_tags")
            or bool(set(rule["required_any_tags"]) & garment_tags)
        )
        and not (set(rule.get("excludes_tags", [])) & garment_tags)
        and garment_compatible_with_template_stages(template, slot, garment)
        and (
            content_mode != "sfw"
            or not visible_in_sfw
            or not (garment_tags & SFW_BLOCKED_GARMENT_TAGS)
        )
    )


def hands_required(item: dict[str, Any] | None) -> int:
    if not item:
        return 0
    if "hands_required" in item:
        return int(item["hands_required"])
    wording = item.get("prompt", "").casefold()
    if any(term in wording for term in (
        "both hands", "her hands", "on hands and knees", "on all fours",
        "leaning back on her hands", "squeezing both breasts", "both nipples",
    )):
        return 2
    if any(term in wording for term in (
        "one hand", "her hand", "with a hand", "fingertip", "finger", "touching",
        "adjusting", "holding", "cupping", "pinching", "tracing", "lifting",
        "pulling", "sliding", "smoothing",
    )):
        return 1
    return 0


def lowerwear_covers_legs(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    wording = f"{item['id']} {item.get('prompt', '')}".casefold()
    return any(term in wording for term in (
        "jeans", "trouser", "pants", "leggings", "joggers", "sweatpants",
    ))


def legwear_extends_above_ankle(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    wording = f"{item['id']} {item.get('prompt', '')}".casefold()
    return bool(tags(item) & {"pantyhose", "stockings"}) or any(
        term in wording for term in (
            "tights", "knee-high", "knee socks", "kneesocks", "over-knee",
            "over-the-knee", "thigh-high", "thigh stockings",
        )
    )


def hosiery_support_belt(outfit: dict[str, Any]) -> dict[str, Any] | None:
    accessory = outfit.get("garments", {}).get("accessories")
    return accessory if accessory and "hosiery_support_belt" in tags(accessory) else None


def validate_outfit_layers(db: dict[str, Any], outfit: dict[str, Any]) -> None:
    template_id = outfit["template"]["id"]
    garments = outfit["garments"]
    optional_relations = db["settings"]["wardrobe_compatibility"]["optional_inner_layers"]
    for relation in optional_relations:
        inner_slot = relation["inner_slot"]
        forced_absent = float(relation["chance"]) == 1
        matching_outer = any(
            outer_slot in garments
            and tags(garments[outer_slot]) & set(relation["outer_tags_any"])
            for outer_slot in relation["outer_slots"]
        )
        if forced_absent and matching_outer and inner_slot in garments:
            raise AppError(
                f"Garment slot {inner_slot} is incompatible with the selected "
                "outer garment construction"
            )
    for slot, slot_rule in outfit["template"]["slots"].items():
        relations = [
            relation for relation in optional_relations
            if relation["inner_slot"] == slot
        ]
        if not slot_rule.get("required", False) or slot in garments:
            continue
        if not relations or not any(
            outer_slot in garments
            for relation in relations for outer_slot in relation["outer_slots"]
        ):
            continue
        permitted = any(
            any(
                outer_slot in garments
                and tags(garments[outer_slot]) & set(relation["outer_tags_any"])
                for outer_slot in relation["outer_slots"]
            )
            for relation in relations
        )
        if not permitted:
            raise AppError(
                f"Required garment slot {slot} is missing from outfit {template_id}"
            )
    incompatible_categories = [
        item["id"] for item in garments.values()
        if not category_allows(outfit["template"], item)
    ]
    if incompatible_categories:
        raise AppError(
            f"Normal outfit {template_id} cannot contain luxury garments: "
            f"{sorted(incompatible_categories)}"
        )
    lowerwear = garments.get("lowerwear")
    legwear = garments.get("legwear")
    if lowerwear_covers_legs(lowerwear) and legwear_extends_above_ankle(legwear):
        raise AppError(
            f"Garment layers {legwear['id']} with {lowerwear['id']} are incompatible: "
            "long legwear cannot be composed over leg-covering trousers"
        )
    support_mode = (legwear or {}).get("support_mode")
    support_belt = hosiery_support_belt(outfit)
    if support_mode == "garter_required" and not support_belt:
        raise AppError(
            f"Hosiery {legwear['id']} requires a compatible garter support belt"
        )
    if support_belt and support_mode != "garter_required":
        raise AppError(
            f"Garter support belt {support_belt['id']} requires garter-supported stockings"
        )
    for rule in db["settings"].get("garment_layer_rules", []):
        if template_id not in rule["template_ids"]:
            continue
        outer = garments.get(rule["outer_slot"])
        inner = garments.get(rule["inner_slot"])
        if not outer or not inner:
            continue
        if (
            outer["id"] not in rule["allowed_outer_ids"]
            or inner["id"] not in rule["allowed_inner_ids"]
        ):
            raise AppError(
                f"Garment layers {inner['id']} under {outer['id']} are incompatible"
            )


def validate_outfit_color_groups(outfit: dict[str, Any]) -> None:
    """Keep every selected garment in one authored color group exactly matched."""
    garments = outfit["garments"]
    colors = outfit.get("colors", {})
    grouped: dict[str, list[tuple[str, str]]] = {}
    for slot, rule in outfit["template"]["slots"].items():
        group = rule.get("color_group")
        if not group or slot not in garments:
            continue
        color = colors.get(slot)
        if color is None:
            raise AppError(f"Garment slot {slot} is missing its coordinated color")
        grouped.setdefault(group, []).append((slot, color["id"]))
    for group, selected in grouped.items():
        color_ids = {color_id for _, color_id in selected}
        if len(color_ids) > 1:
            details = ", ".join(
                f"{slot}={color_id}" for slot, color_id in selected
            )
            raise AppError(
                f"Outfit color group {group} must use one exact color: {details}"
            )


def set_outfit_group_color(
    db: dict[str, Any], outfit: dict[str, Any], slot: str,
    requested_color_id: str | None = None,
) -> None:
    """Set one slot or its complete authored group to a shared allowed color."""
    rule = outfit["template"]["slots"][slot]
    group = rule.get("color_group")
    grouped_slots = [
        candidate_slot
        for candidate_slot, candidate_rule in outfit["template"]["slots"].items()
        if candidate_slot in outfit["garments"]
        and (
            candidate_rule.get("color_group") == group
            if group else candidate_slot == slot
        )
    ]
    enabled_colors = {
        item["id"]: item for item in db["colors"] if not item.get("disabled", False)
    }
    shared = set(enabled_colors)
    for candidate_slot in grouped_slots:
        garment = outfit["garments"][candidate_slot]
        shared &= set(garment.get("allowed_colors") or enabled_colors)
    if not shared:
        raise AppError(
            f"Outfit color group {group or slot} has no shared enabled color"
        )
    if requested_color_id is not None:
        if requested_color_id not in shared:
            raise AppError(
                f"Color {requested_color_id} is incompatible with coordinated "
                f"outfit group {group or slot}"
            )
        selected_id = requested_color_id
    else:
        current_ids = {
            outfit["colors"][candidate_slot]["id"]
            for candidate_slot in grouped_slots
            if candidate_slot in outfit["colors"]
        }
        selected_id = (
            next(iter(current_ids))
            if len(current_ids) == 1 and current_ids <= shared else
            sorted(shared)[0]
        )
    for candidate_slot in grouped_slots:
        outfit["colors"][candidate_slot] = enabled_colors[selected_id]


def garment_allowed_by_layer_rules(
    db: dict[str, Any], template_id: str, slot: str, garment_id: str
) -> bool:
    for rule in db["settings"].get("garment_layer_rules", []):
        if template_id not in rule["template_ids"]:
            continue
        if slot == rule["outer_slot"] and garment_id not in rule["allowed_outer_ids"]:
            return False
        if slot == rule["inner_slot"] and garment_id not in rule["allowed_inner_ids"]:
            return False
    return True


def surface_modifier_candidates(
    db: dict[str, Any], furniture: dict[str, Any], kind: str
) -> list[dict[str, Any]]:
    if kind not in {"color", "texture"}:
        raise AppError(f"Unknown surface modifier kind: {kind}")
    if not furniture.get(f"surface_{kind}_target"):
        return []
    settings = db["settings"].get("surface_modifiers", {})
    ids = set(settings.get("colors" if kind == "color" else "textures", []))
    section = "colors" if kind == "color" else "fabric_textures"
    return [
        item for item in db[section]
        if item["id"] in ids and not item.get("disabled", False)
    ]


def compatible_with_requirements(item: dict[str, Any], available_tags: set[str]) -> bool:
    required_any = set(item.get("requires_any_tags", []))
    return (
        set(item.get("requires_tags", [])).issubset(available_tags)
        and (not required_any or bool(required_any & available_tags))
        and not (set(item.get("excludes_tags", [])) & available_tags)
    )


def recipe_reference_ids(recipe: dict[str, Any] | None, field: str) -> list[str]:
    if not recipe:
        return []
    options = recipe.get(f"{field}_options")
    reference = recipe.get(field)
    return list(options) if options else ([reference] if reference else [])


INTIMATE_SHOT_SIZE_IDS = {
    "shot_three_quarter", "shot_torso_closeup", "shot_intimate_macro",
}


def validate_camera_grammar(scene: dict[str, Any]) -> None:
    """Reject semantically contradictory cross-field camera combinations."""
    shot_size = scene["shot_size"]
    angle = scene["camera_angle"]
    framing = scene["framing"]
    focus = scene["focus_target"]
    recipe = scene.get("explicit_recipe")
    action = scene["action"]

    def conflict(reason: str, *items: dict[str, Any] | None) -> None:
        ids = [item["id"] for item in items if item]
        raise AppError(f"Camera conflict [{', '.join(ids)}]: {reason}")

    if shot_size["id"] == "shot_intimate_macro":
        if framing["id"] == "framing_environmental":
            conflict(
                "intimate macro cannot use environmental framing",
                shot_size, framing,
            )
        if focus["id"] != "focus_intimate":
            conflict(
                "intimate macro requires intimate focus",
                shot_size, focus,
            )

    rear_display = (
        focus["id"] == "focus_rear"
        or recipe is not None and recipe.get("focus_target") == "focus_rear"
        or scene["stage"].get("plateau_kind") == "provocative_rear"
    )
    if rear_display:
        if "rear_angle" not in tags(angle):
            conflict("rear display requires a rear-compatible angle", recipe, angle)
        if focus["id"] != "focus_rear":
            conflict("rear display requires rear focus", recipe, focus)
        if framing["id"] == "framing_environmental":
            conflict(
                "rear display cannot use environmental framing",
                recipe, framing,
            )

    intimate_action = (
        bool(tags(action) & {"masturbation_action", "explicit_intimate_action"})
        or recipe is not None and recipe.get("focus_target") == "focus_intimate"
    )
    if intimate_action:
        if focus["id"] != "focus_intimate":
            conflict(
                "intimate action requires intimate focus",
                recipe, action, focus,
            )
        if shot_size["id"] not in INTIMATE_SHOT_SIZE_IDS:
            conflict(
                "intimate action requires three-quarter or closer treatment",
                recipe, action, shot_size,
            )
        if framing["id"] == "framing_environmental":
            conflict(
                "intimate action cannot use environmental framing",
                recipe, action, framing,
            )


def camera_candidate_compatible(
    scene: dict[str, Any], key: str, candidate: dict[str, Any]
) -> bool:
    trial = dict(scene)
    trial[key] = candidate
    try:
        validate_camera_grammar(trial)
    except AppError:
        return False
    return True


def matching_location_zones(
    db: dict[str, Any], interior: dict[str, Any], furniture: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return concrete location alternatives compatible with one furnishing."""
    furniture_tags = tags(furniture)
    interior_tags = tags(interior)
    specific: list[dict[str, Any]] = []
    generic: list[dict[str, Any]] = []
    for zone in db["location_zones"]:
        terms = zone.get("match_id_terms", [])
        match_tags = set(zone.get("match_any_tags", []))
        if terms:
            matched = any(term in furniture["id"] for term in terms)
            target = specific
        else:
            matched = not match_tags or bool(match_tags & furniture_tags)
            target = generic
        if not matched:
            continue
        allowed = set(zone.get("environment_tags", []))
        if not allowed or allowed & interior_tags:
            target.append(zone)
    return specific or generic


def resolve_location_zone(
    db: dict[str, Any], interior: dict[str, Any], furniture: dict[str, Any],
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Resolve one explicit physical zone without combining alternatives."""
    candidates = matching_location_zones(db, interior, furniture)
    if candidates:
        return weighted_choice(rng, candidates) if rng else candidates[0]
    raise AppError(f"No location zone matches furniture {furniture['id']}")


def validate_pose_zone(pose: dict[str, Any], zone: dict[str, Any]) -> None:
    pose_tags = tags(pose)
    needed = set()
    if pose_tags & {"lying", "reclining", "all_fours"}:
        needed.add("reclining")
    if "sitting" in pose_tags:
        needed.add("seated")
    if "kneeling" in pose_tags:
        needed.add("kneeling")
    if pose_tags & {"standing", "bent_over"}:
        needed.add("standing")
    if pose_tags & {"closeup", "intimate_closeup", "breast_focus"}:
        needed.add("closeup")
    missing = needed - set(zone.get("capabilities", []))
    if missing:
        raise AppError(
            f"Location zone conflict [{zone['id']}, {pose['id']}]: "
            f"missing capabilities {sorted(missing)}"
        )


NSFW_LEVELS = ("topless", "nude", "explicit")
INTENSITY_LEVELS = ("fashion", "sensual", "erotic", "nude", "explicit", "peak")
STAGE_INTENSITIES = {
    "covered": ("fashion", "sensual"),
    "lingerie": ("sensual", "erotic"),
    "topless": ("erotic", "nude"),
    "nude": ("nude", "explicit"),
    "explicit": ("explicit", "peak"),
}


def allowed_scene_intensities(scene: dict[str, Any]) -> tuple[str, ...]:
    recipe = scene.get("explicit_recipe")
    if recipe and recipe.get("intensity"):
        return (recipe["intensity"],)
    return STAGE_INTENSITIES.get(scene["stage"]["level"], INTENSITY_LEVELS)


def item_allowed_intensities(item: dict[str, Any]) -> set[str]:
    explicit = item.get("allowed_intensities")
    if explicit:
        return set(explicit)
    levels = item.get("allowed_levels")
    if not levels:
        return set(INTENSITY_LEVELS)
    return set().union(*(STAGE_INTENSITIES.get(level, ()) for level in levels))


def item_allows_intensity(item: dict[str, Any], intensity: str) -> bool:
    return intensity in item_allowed_intensities(item)


SFW_BLOCKED_VISIBILITY = {"breasts", "nipples", "pubic_area", "genitals"}
SFW_BLOCKED_GARMENT_TAGS = {
    "explicit", "erotic", "exposing", "sheer", "transparent", "open_cup", "crotchless",
}
SFW_BLOCKED_DIRECTION_TAGS = {
    "explicit_pose", "erotic_pose", "masturbation_pose", "open_legs",
    "explicit_action", "erotic_action", "masturbation_action", "undressing_action",
    "provocative_action", "provocative_rear",
}


def is_sfw_stage(stage: dict[str, Any]) -> bool:
    """Return whether a stage guarantees a fully covered composition."""
    return (
        stage.get("level") == "covered"
        and not (set(stage.get("body_visibility", [])) & SFW_BLOCKED_VISIBILITY)
    )


def template_supports_sfw(db: dict[str, Any], template: dict[str, Any]) -> bool:
    """Return whether the template has a realizable fully covered outfit."""
    safe_stages = [
        stage for stage in effective_photoshoot_stages(template)
        if is_sfw_stage(stage)
    ]
    if not safe_stages:
        return False
    candidates: dict[str, list[dict[str, Any]]] = {
        slot: [
            garment for garment in db["garments"][rule["catalog"]]
            if garment_matches_template_slot(db, template, slot, garment, "sfw")
        ]
        for slot, rule in template["slots"].items()
    }
    if any(
        rule.get("required", False) and not candidates[slot]
        for slot, rule in template["slots"].items()
    ):
        return False
    for stage in safe_stages:
        visible = set(stage.get("visible_slots", []))
        chest = visible & {"upperwear", "full_body", "outerwear", "bra"}
        genitals = visible & {"lowerwear", "full_body", "panties"}
        if not any(template["slots"][slot].get("required", False) and candidates[slot] for slot in chest):
            return False
        if not any(template["slots"][slot].get("required", False) and candidates[slot] for slot in genitals):
            return False
    return True


def validate_sfw_outfit(outfit: dict[str, Any]) -> None:
    template = outfit["template"]
    stages = [stage for stage in effective_photoshoot_stages(template) if is_sfw_stage(stage)]
    if not stages:
        raise AppError(f"Outfit template {template['id']} has no SFW-compatible covered stage")
    garments = outfit["garments"]
    for stage in stages:
        visible_slots = set(stage.get("visible_slots", []))
        visible = {
            slot: garment for slot, garment in garments.items() if slot in visible_slots
        }
        unsafe = [
            garment["id"] for garment in visible.values()
            if tags(garment) & SFW_BLOCKED_GARMENT_TAGS
        ]
        chest_covered = bool(visible_slots & {"upperwear", "full_body", "outerwear", "bra"} & set(visible))
        genitals_covered = bool(visible_slots & {"lowerwear", "full_body", "panties"} & set(visible))
        if unsafe or not chest_covered or not genitals_covered:
            raise AppError(
                f"Outfit template {template['id']} stage {stage['id']} is not fully opaque and covered"
            )


def effective_photoshoot_stages(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Return configured stages plus generic terminal NSFW stages when absent."""
    stages = copy.deepcopy(template["stages"])
    levels = {stage["level"] for stage in stages}
    terminal_specs = {
        "topless": {
            "visible_slots": ["panties", "legwear", "footwear", "accessories"],
            "body_visibility": ["breasts", "nipples"],
        },
        "nude": {
            "visible_slots": ["legwear", "footwear", "accessories"],
            "body_visibility": ["breasts", "nipples", "pubic_area", "genitals"],
        },
        "explicit": {
            "visible_slots": ["legwear", "footwear", "accessories"],
            "body_visibility": ["breasts", "nipples", "pubic_area", "genitals"],
        },
    }
    template_slots = set(template["slots"])
    for level in NSFW_LEVELS:
        if level in levels:
            continue
        spec = terminal_specs[level]
        stages.append({
            "id": f"{template['id']}_{level}",
            "level": level,
            "visible_slots": [slot for slot in spec["visible_slots"] if slot in template_slots],
            "body_visibility": spec["body_visibility"],
        })
    safe = [stage for stage in stages if stage["level"] not in NSFW_LEVELS]
    nsfw = sorted(
        (stage for stage in stages if stage["level"] in NSFW_LEVELS),
        key=lambda stage: NSFW_LEVELS.index(stage["level"]),
    )
    return safe + nsfw


def progressive_stage(stages: list[dict[str, Any]], index: int, count: int) -> dict[str, Any]:
    if count <= len(stages):
        return stages[len(stages) - count + index]
    return stages[min(len(stages) - 1, index * len(stages) // count)]


HUMAN_SELECTION_ORDER = (
    "age", "ethnic_appearance", "skin_tone", "skin_marking", "face_shape", "eye_shape",
    "eye_color", "eyebrows", "nose", "lips", "cheekbones", "jawline",
    "hair_texture", "hair_length", "hair_style", "hair_color", "height",
    "body_frame", "body_state", "waist", "hips", "breast_size", "breast_shape",
    "areola_size", "areola_color", "nipple_size", "nipple_shape",
    "pubic_hair", "genital_appearance", "facial_accents", "makeup",
    "manicure",
)


class Composer:
    def __init__(
        self,
        db: dict[str, Any],
        rng: random.Random,
        use_curated_defaults: bool = True,
    ):
        self.db = db
        self.rng = rng
        self.use_curated_defaults = bool(use_curated_defaults)
        self.max_scene_attempts = load_config()[0]["limits"]["max_scene_attempts"]
        self.colors = {
            item["id"]: item for item in db["colors"] if not item.get("disabled", False)
        }
        self.item_index = {
            item["id"]: item
            for item in iter_content_items(db)
            if not item.get("disabled", False)
        }
        self._category_bags: dict[str, list[str]] = {}
        self._selection_bags: dict[str, list[str]] = {}
        self._selection_signatures: dict[str, tuple[str, ...]] = {}
        self._selection_last: dict[str, str] = {}

    def choose_from_bag(
        self, key: str, candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Choose by seeded weighted shuffle bag, avoiding refill-boundary repeats."""
        if not candidates:
            raise AppError(f"No compatible candidates for {key}")
        by_id = {item["id"]: item for item in candidates}
        signature = tuple(sorted(by_id))
        bag = self._selection_bags.get(key, [])
        if self._selection_signatures.get(key) != signature:
            bag = []
        if not bag:
            remaining = list(candidates)
            bag = []
            while remaining:
                selected = weighted_choice(self.rng, remaining)
                bag.append(selected["id"])
                remaining.remove(selected)
            last = self._selection_last.get(key)
            if last and len(bag) > 1 and bag[-1] == last:
                bag[0], bag[-1] = bag[-1], bag[0]
            self._selection_bags[key] = bag
            self._selection_signatures[key] = signature
        selected_id = bag.pop()
        self._selection_last[key] = selected_id
        return by_id[selected_id]

    def choose_catalog_category(self, key: str, allowed: set[str]) -> str:
        """Cycle every enabled tier before repeating, with seeded random order."""
        bag = self._category_bags.get(key, [])
        if not bag or not set(bag).issubset(allowed):
            bag = sorted(allowed)
            self.rng.shuffle(bag)
            self._category_bags[key] = bag
        return bag.pop()

    def choose_garment_modifier(
        self, section: str, garment: dict[str, Any], chance: float
    ) -> dict[str, Any] | None:
        if self.rng.random() >= chance:
            return None
        candidates = [
            item for item in self.db[section]
            if not item.get("disabled", False)
            and garment["id"] in item["allowed_garment_ids"]
        ]
        return weighted_choice(self.rng, candidates) if candidates else None

    def surface_style(
        self,
        fixed: dict[str, Any],
        furniture: dict[str, Any],
        overrides: dict[str, str],
    ) -> dict[str, dict[str, Any] | None]:
        styles = fixed.setdefault("surface_styles", {})
        continuity_tags = (
            "bed", "sofa", "chair", "bathtub", "shower", "counter",
            "windowsill", "rug", "floor", "wall", "bench", "lounger",
            "blanket", "tree", "deck",
        )
        family = next(
            (tag for tag in continuity_tags if tag in tags(furniture)),
            furniture["id"],
        )
        continuity_key = f"{fixed['interior']['id']}:{family}"
        if continuity_key not in styles:
            settings = self.db["settings"].get("surface_modifiers", {})
            style: dict[str, dict[str, Any] | None] = {}
            for kind in ("color", "texture"):
                candidates = surface_modifier_candidates(self.db, furniture, kind)
                chance = float(settings.get(f"{kind}_chance", 0))
                style[f"surface_{kind}"] = (
                    weighted_choice(self.rng, candidates)
                    if candidates and self.rng.random() < chance else None
                )
            styles[continuity_key] = style
        style = dict(styles[continuity_key])
        for kind in ("color", "texture"):
            key = f"surface_{kind}"
            if key not in overrides:
                continue
            wanted = overrides[key]
            candidates = surface_modifier_candidates(self.db, furniture, kind)
            selected = next((item for item in candidates if item["id"] == wanted), None)
            if selected is None:
                raise AppError(f"Surface {kind} is incompatible with {furniture['id']}")
            style[key] = selected
        return style

    def choose_human(
        self,
        overrides: dict[str, dict[str, Any]] | None = None,
        use_default_ethnicity: bool = True,
        use_human_defaults: bool | None = None,
    ) -> dict[str, Any]:
        overrides = dict(overrides or {})
        if use_human_defaults is None:
            use_human_defaults = self.use_curated_defaults
        human_defaults = self.db["settings"].get("human_defaults", {})
        default_pools = (
            dict(human_defaults.get("pools", {})) if use_human_defaults else {}
        )
        if not use_default_ethnicity:
            default_pools.pop("ethnic_appearance", None)
        human: dict[str, Any] = {}
        parts = self.db["human_model_parts"]
        order = [category for category in HUMAN_SELECTION_ORDER if category in parts]
        order.extend(
            category for category in parts if category not in HUMAN_SELECTION_ORDER
        )
        selected_tags: set[str] = set()
        for category in order:
            if category == "facial_accents":
                candidates = [
                    item for item in parts[category]
                    if not item.get("disabled", False)
                    and compatible_with_requirements(item, selected_tags)
                ]
                if default_pools.get(category):
                    allowed_ids = set(default_pools[category])
                    candidates = [item for item in candidates if item["id"] in allowed_ids]
                def accent_family(item: dict[str, Any]) -> str:
                    identifier = item["id"]
                    for family in ("freckle", "mole", "mark", "lash", "skin"):
                        if family in identifier:
                            return "mark" if family in {"mole", "mark"} else family
                    return identifier

                selected_accents: list[dict[str, Any]] = []
                if candidates:
                    selected_accents.append(weighted_choice(self.rng, candidates))
                    second = [
                        item for item in candidates
                        if accent_family(item) != accent_family(selected_accents[0])
                    ]
                    if second and self.rng.random() < 0.35:
                        selected_accents.append(weighted_choice(self.rng, second))
                human[category] = selected_accents
                for item in human[category]:
                    selected_tags |= tags(item)
                continue
            candidates = [
                item for item in parts[category]
                if not item.get("disabled", False)
                and compatible_with_requirements(item, selected_tags)
            ]
            if category not in overrides and default_pools.get(category):
                allowed_ids = set(default_pools[category])
                candidates = [item for item in candidates if item["id"] in allowed_ids]
            if category in overrides:
                choice = overrides[category]
                if choice not in candidates:
                    raise AppError(
                        f"Human trait override {choice['id']} is incompatible in category {category}"
                    )
            else:
                if not candidates:
                    raise AppError(
                        f"No compatible enabled default candidates remain for human category {category}"
                    )
                choice = weighted_choice(self.rng, candidates)
            human[category] = choice
            selected_tags |= tags(choice)
        return human

    def choose_template(
        self, selected_category: str | None = None, content_mode: str = "progressive"
    ) -> dict[str, Any]:
        allowed = set(
            self.db["settings"]["scene_defaults"]["wardrobe_categories"]
        )
        if self.use_curated_defaults:
            selected_category = selected_category or self.choose_catalog_category(
                "wardrobe", allowed
            )
        candidates = [
            template for template in self.db["outfit_templates"]
            if not template.get("disabled", False)
            and (
                selected_category is None
                or template["catalog_category"] == selected_category
            )
            and (content_mode != "sfw" or template_supports_sfw(self.db, template))
        ]
        return weighted_choice(self.rng, candidates)

    def _choose_outfit_once(
        self, template: dict[str, Any], content_mode: str = "progressive"
    ) -> dict[str, Any]:
        selected: dict[str, dict[str, Any]] = {}
        group_tags: dict[str, set[str]] = {}
        for slot, rule in template["slots"].items():
            required = bool(rule.get("required", False))
            if not required and self.rng.random() > float(rule.get("chance", 1)):
                continue
            candidates = [
                item for item in self.db["garments"][rule["catalog"]]
                if garment_matches_template_slot(
                    self.db, template, slot, item, content_mode
                )
            ]
            match_group = rule.get("match_group")
            if match_group and match_group in group_tags:
                candidates = [
                    item for item in candidates
                    if set(item.get("mix_tags", item.get("tags", []))) & group_tags[match_group]
                ]
            if self.use_curated_defaults and catalog_category(template) == "luxury":
                candidates = prefer_catalog_category(candidates, "luxury")
            choice = weighted_choice(self.rng, candidates)
            selected[slot] = choice
            if match_group:
                mix = set(choice.get("mix_tags", choice.get("tags", [])))
                group_tags[match_group] = group_tags.get(match_group, mix) & mix

        resolved_template = copy.deepcopy(template)
        compatibility = self.db["settings"]["wardrobe_compatibility"]
        for relation in compatibility["optional_inner_layers"]:
            inner_slot = relation["inner_slot"]
            if inner_slot not in selected:
                continue
            compatible_outer = next((
                slot for slot in relation["outer_slots"]
                if slot in selected
                and tags(selected[slot]) & set(relation["outer_tags_any"])
            ), None)
            if compatible_outer is None or self.rng.random() >= float(relation["chance"]):
                continue
            selected.pop(inner_slot)
            droppable_levels = set(relation["drop_uncovered_stage_levels"])
            coverage_slots = set(relation["coverage_slots"])
            resolved_template["stages"] = [
                stage for stage in resolved_template["stages"]
                if not (
                    stage["level"] in droppable_levels
                    and not (
                        set(stage.get("visible_slots", []))
                        & coverage_slots
                        & set(selected)
                    )
                )
            ]

        for stage in resolved_template["stages"]:
            visible_slots = set(stage.get("visible_slots", []))
            for relation in compatibility["stage_visibility_rules"]:
                matching_outer = any(
                    slot in visible_slots
                    and slot in selected
                    and tags(selected[slot]) & set(relation["outer_tags_any"])
                    for slot in relation["outer_slots"]
                )
                if not matching_outer:
                    continue
                hidden = set(relation["hide_slots"])
                removed_visibility = set(relation["remove_body_visibility"])
                stage["visible_slots"] = [
                    slot for slot in stage.get("visible_slots", []) if slot not in hidden
                ]
                stage["body_visibility"] = [
                    part for part in stage.get("body_visibility", [])
                    if part not in removed_visibility
                ]
                visible_slots = set(stage["visible_slots"])

        occupied: dict[str, str] = {}
        for slot, item in selected.items():
            for occupied_slot in item.get("occupies_slots", [slot]):
                if occupied_slot in occupied and occupied[occupied_slot] != item["id"]:
                    raise AppError(f"Outfit slot conflict: {occupied[occupied_slot]} and {item['id']}")
                occupied[occupied_slot] = item["id"]

        validate_outfit_layers(
            self.db,
            {"template": resolved_template, "garments": selected},
        )

        assigned_colors: dict[str, dict[str, Any]] = {}
        grouped_slots: dict[str, list[str]] = {}
        for slot in selected:
            group = resolved_template["slots"][slot].get("color_group")
            if group:
                grouped_slots.setdefault(group, []).append(slot)
        color_groups: dict[str, str] = {}
        for group, slots in grouped_slots.items():
            shared = set(self.colors)
            for slot in slots:
                shared &= set(selected[slot].get("allowed_colors") or self.colors)
            if not shared:
                raise AppError(f"Outfit color group '{group}' has no color shared by slots {slots}")
            color_groups[group] = self.rng.choice(sorted(shared))
        for slot, item in selected.items():
            allowed = [
                color_id
                for color_id in (item.get("allowed_colors") or list(self.colors))
                if color_id in self.colors
            ]
            rule = resolved_template["slots"][slot]
            group = rule.get("color_group")
            if group:
                color_id = color_groups[group]
            else:
                color_id = self.rng.choice(allowed)
            assigned_colors[slot] = self.colors[color_id]

        covered_inner_slots: set[str] = set()
        active_relations: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for relation in compatibility["visible_layer_rules"]:
            outer_slot = next((
                slot for slot in relation["outer_slots"]
                if slot in selected
                and tags(selected[slot]) & set(relation["outer_tags_any"])
            ), None)
            inner_slot = relation["inner_slot"]
            if outer_slot is None or inner_slot not in selected:
                continue
            active_relations.setdefault(inner_slot, []).append((outer_slot, relation))

        for inner_slot, constraints in active_relations.items():
            outer_ids = [assigned_colors[outer_slot]["id"] for outer_slot, _ in constraints]
            required_families = {color_family(self.db, color_id) for color_id in outer_ids}
            if len(required_families) != 1:
                raise AppError(
                    f"Visible layers for {inner_slot} require conflicting color families: "
                    f"{sorted(required_families)}"
                )
            required_family = next(iter(required_families))
            allowed_inner = [
                color_id
                for color_id in (selected[inner_slot].get("allowed_colors") or list(self.colors))
                if color_id in self.colors
            ]
            exact = [color_id for color_id in set(outer_ids) if color_id in allowed_inner]
            if len(set(outer_ids)) == 1 and exact:
                chosen = exact[0]
            else:
                tonal = [
                    color_id for color_id in allowed_inner
                    if color_family(self.db, color_id) == required_family
                ]
                if not tonal:
                    raise AppError(
                        f"No {required_family} color compatibility for visible {inner_slot}"
                    )
                chosen = self.rng.choice(sorted(tonal))
            assigned_colors[inner_slot] = self.colors[chosen]
            if any(relation["suppress_inner_pattern"] for _, relation in constraints):
                covered_inner_slots.add(inner_slot)
        modifier_settings = self.db["settings"].get("garment_modifiers", {})
        assigned_patterns: dict[str, dict[str, Any]] = {}
        assigned_textures: dict[str, dict[str, Any]] = {}
        for slot, item in selected.items():
            pattern = self.choose_garment_modifier(
                "patterns", item, float(modifier_settings.get("pattern_chance", 0))
            )
            texture = self.choose_garment_modifier(
                "fabric_textures", item, float(modifier_settings.get("texture_chance", 0))
            )
            if pattern and slot not in covered_inner_slots:
                assigned_patterns[slot] = pattern
            if texture:
                assigned_textures[slot] = texture
        outfit = {
            "template": resolved_template,
            "garments": selected,
            "colors": assigned_colors,
            "patterns": assigned_patterns,
            "textures": assigned_textures,
        }
        validate_outfit_color_groups(outfit)
        return outfit

    def validate_outfit_environment(
        self,
        outfit: dict[str, Any],
        interior: dict[str, Any],
    ) -> None:
        environment_tags = tags(interior)
        for item in outfit["garments"].values():
            required = set(item.get("requires_environment_tags", []))
            excluded = set(item.get("excludes_environment_tags", []))
            if not required.issubset(environment_tags) or excluded & environment_tags:
                raise AppError(
                    f"Garment {item['id']} is incompatible with interior {interior['id']}: "
                    f"required_environment={sorted(required)}, "
                    f"excluded_environment={sorted(excluded)}"
                )

    def validate_outfit_stage_coverage(self, outfit: dict[str, Any]) -> None:
        """A sheer covered layer must reveal the same safe bra used later."""
        garments = outfit["garments"]
        bra = garments.get("bra")
        for stage in outfit["template"].get("stages", []):
            visible_by_slot = {
                slot: garments[slot] for slot in stage.get("visible_slots", [])
                if slot in garments
            }
            visible = list(visible_by_slot.values())
            sheer_chest = any(
                slot in CHEST_GARMENT_SLOTS
                and tags(item) & {"sheer", "transparent"}
                for slot, item in visible_by_slot.items()
            )
            opaque_visible_chest = any(
                slot in CHEST_GARMENT_SLOTS
                and not (tags(item) & OPAQUE_LINGERIE_BLOCKED_TAGS)
                for slot, item in visible_by_slot.items()
            )
            opaque_underlying_bra = (
                bra is not None
                and not (tags(bra) & OPAQUE_LINGERIE_BLOCKED_TAGS)
            )
            if (
                stage.get("level") == "covered"
                and sheer_chest
                and not (opaque_visible_chest or opaque_underlying_bra)
            ):
                raise AppError(
                    "A covered sheer outfit requires one opaque non-explicit bra beneath it"
                )
            if (
                stage.get("level") == "lingerie"
                and bra in visible
                and not garment_compatible_with_template_stages(
                    outfit["template"], "bra", bra
                )
            ):
                raise AppError(
                    "A lingerie stage with covered anatomy requires an opaque bra"
                )
            panties = garments.get("panties")
            if (
                stage.get("level") == "lingerie"
                and panties in visible
                and not garment_compatible_with_template_stages(
                    outfit["template"], "panties", panties
                )
            ):
                raise AppError(
                    "A lingerie stage with covered anatomy requires opaque panties"
                )

    def choose_outfit(
        self,
        template: dict[str, Any],
        interior: dict[str, Any] | None = None,
        content_mode: str = "progressive",
    ) -> dict[str, Any]:
        attempts = self.max_scene_attempts
        last_error = "no compatible outfit"
        for _ in range(attempts):
            try:
                outfit = self._choose_outfit_once(template, content_mode)
                self.validate_outfit_stage_coverage(outfit)
                if content_mode == "sfw":
                    validate_sfw_outfit(outfit)
                if interior is not None:
                    self.validate_outfit_environment(outfit, interior)
                return outfit
            except AppError as exc:
                last_error = str(exc)
        raise AppError(
            f"Could not resolve outfit template {template['id']} after {attempts} attempts: {last_error}"
        )

    def fixed_context(self, content_mode: str = "progressive") -> dict[str, Any]:
        attempts = self.max_scene_attempts
        last_error = "no compatible fixed context"
        allowed_wardrobes = set(
            self.db["settings"]["scene_defaults"]["wardrobe_categories"]
        )
        selected_wardrobe_category = (
            self.choose_catalog_category("wardrobe", allowed_wardrobes)
            if self.use_curated_defaults else None
        )
        allowed_environments = set(
            self.db["settings"]["scene_defaults"]["environment_categories"]
        )
        selected_environment_category = (
            self.choose_catalog_category("environment", allowed_environments)
            if self.use_curated_defaults else None
        )
        for _ in range(attempts):
            try:
                template = self.choose_template(selected_wardrobe_category, content_mode)
                interiors = [
                    interior for interior in self.db["interiors"]
                    if not interior.get("disabled", False)
                    and (
                        selected_environment_category is None
                        or catalog_category(interior) == selected_environment_category
                    )
                ]
                scene_pools = (
                    self.db["settings"]["scene_defaults"].get("pools", {})
                    if self.use_curated_defaults else {}
                )
                if scene_pools.get("interiors"):
                    interiors = apply_preferred_pool(interiors, scene_pools["interiors"])
                interior = weighted_choice(self.rng, interiors)
                furniture_candidates = [
                    item for item in self.db["furniture"]
                    if compatible_with_requirements(item, tags(interior))
                    and category_allows(interior, item)
                ]
                if (
                    self.use_curated_defaults
                    and selected_environment_category == "luxury"
                ):
                    furniture_candidates = prefer_catalog_category(
                        furniture_candidates, "luxury"
                    )
                elif scene_pools.get("furniture"):
                    furniture_candidates = apply_preferred_pool(
                        furniture_candidates, scene_pools["furniture"]
                    )
                mood_candidates = self.db["moods"]
                if scene_pools.get("moods"):
                    allowed_ids = set(scene_pools["moods"])
                    mood_candidates = [
                        item for item in mood_candidates if item["id"] in allowed_ids
                    ]
                photography_candidates = self.db["photography_styles"]
                if scene_pools.get("photography_styles"):
                    allowed_ids = set(scene_pools["photography_styles"])
                    photography_candidates = [
                        item for item in photography_candidates if item["id"] in allowed_ids
                    ]
                outfit = self.choose_outfit(template, interior, content_mode)
                return {
                    "human": self.choose_human(),
                    "outfit": outfit,
                    "interior": interior,
                    "furniture": weighted_choice(self.rng, furniture_candidates),
                    "mood": weighted_choice(self.rng, mood_candidates),
                    "photography_style": weighted_choice(self.rng, photography_candidates),
                }
            except AppError as exc:
                last_error = str(exc)
        raise AppError(f"Could not resolve a compatible fixed context after {attempts} attempts: {last_error}")

    def variable_context(
        self,
        stage: dict[str, Any],
        fixed: dict[str, Any],
        overrides: dict[str, str] | None = None,
        avoid: dict[str, set[str]] | None = None,
        bag_scope: str | None = None,
    ) -> dict[str, Any]:
        overrides = overrides or {}
        avoid = avoid or {}
        family = (
            stage.get("visual_category")
            or stage.get("plateau_kind")
            or stage["level"]
        )

        def choose(section: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
            if bag_scope:
                return self.choose_from_bag(
                    f"{bag_scope}:{family}:{section}", candidates
                )
            fresh = [item for item in candidates if item["id"] not in avoid.get(section, set())]
            return weighted_choice(self.rng, fresh or candidates)

        furniture_candidates = [
            item for item in self.db["furniture"]
            if not item.get("disabled", False)
            and compatible_with_requirements(item, tags(fixed["interior"]))
            and category_allows(fixed["interior"], item)
        ]
        scene_pools = (
            self.db["settings"]["scene_defaults"].get("pools", {})
            if self.use_curated_defaults else {}
        )
        luxury_environment = catalog_category(fixed["interior"]) == "luxury"
        if (
            self.use_curated_defaults
            and not overrides.get("furniture")
            and luxury_environment
        ):
            furniture_candidates = prefer_catalog_category(
                furniture_candidates, "luxury"
            )
        elif not overrides.get("furniture") and scene_pools.get("furniture"):
            furniture_candidates = apply_preferred_pool(
                furniture_candidates, scene_pools["furniture"]
            )
        if overrides.get("furniture"):
            furniture_candidates = [item for item in furniture_candidates if item["id"] == overrides["furniture"]]
        furniture = choose("furniture", furniture_candidates)
        location_zone = resolve_location_zone(
            self.db, fixed["interior"], furniture, self.rng
        )
        surface_style = self.surface_style(fixed, furniture, overrides)
        available_tags = set(stage.get("body_visibility", [])) | {stage["level"]}
        if stage.get("visual_category"):
            available_tags.add(stage["visual_category"])
        available_tags |= set(stage.get("visible_slots", []))
        available_tags |= tags(furniture) | tags(fixed["interior"])
        available_tags |= set(location_zone.get("capabilities", []))
        visible_slots = set(stage.get("visible_slots", []))
        available_tags |= set().union(*(
            tags(item) for slot, item in fixed["outfit"]["garments"].items()
            if slot in visible_slots
        ), set())
        recipe = None
        if stage["level"] == "explicit":
            recipes = [item for item in self.db["explicit_recipes"] if not item.get("disabled", False)]
            if stage.get("plateau_kind"):
                recipes = [item for item in recipes if item.get("plateau_kind") == stage["plateau_kind"]]
            if overrides.get("explicit_recipe"):
                recipes = [item for item in recipes if item["id"] == overrides["explicit_recipe"]]
            if overrides.get("intensity"):
                recipes = [
                    item for item in recipes
                    if item.get("intensity", "explicit") == overrides["intensity"]
                ]
            if recipes:
                recipe = choose("explicit_recipe", recipes)
                available_tags |= tags(recipe)
        intensity = overrides.get("intensity") or (recipe.get("intensity", "explicit") if recipe else {
            "covered": "fashion", "lingerie": "sensual", "topless": "erotic",
            "nude": "nude", "explicit": "explicit",
        }.get(stage["level"], "fashion"))
        poses = [
            item for item in self.db["poses"]
            if stage["level"] in item.get("allowed_levels", [stage["level"]])
            and item_allows_intensity(item, intensity)
            and compatible_with_requirements(item, available_tags)
        ]
        if stage.get("sfw"):
            poses = [item for item in poses if not (tags(item) & SFW_BLOCKED_DIRECTION_TAGS)]
        if stage["level"] == "explicit":
            poses = [item for item in poses if "explicit_pose" in tags(item)]
        elif stage["level"] in {"topless", "nude"}:
            nsfw_pose_tags = {"erotic_pose", "topless_pose", "nude_pose", "open_legs"}
            poses = [item for item in poses if tags(item) & nsfw_pose_tags]
        plateau_kind = stage.get("plateau_kind") or (
            recipe.get("plateau_kind") if recipe else None
        )
        if plateau_kind == "provocative_rear":
            poses = [item for item in poses if "provocative_rear" in tags(item)]
        elif plateau_kind == "intimate_closeup":
            poses = [item for item in poses if "intimate_closeup" in tags(item)]
        elif plateau_kind == "masturbation":
            poses = [item for item in poses if "masturbation_pose" in tags(item)]
        elif plateau_kind == "panties_aside":
            poses = [
                item for item in poses
                if "open_legs" in tags(item) and "provocative_rear" not in tags(item)
            ]
        if recipe and recipe.get("pose_tags"):
            required = set(recipe["pose_tags"])
            poses = [item for item in poses if required.issubset(tags(item))]
        poses = [item for item in poses if recipe_focus_compatible(item, recipe, "pose")]
        if overrides.get("pose"):
            poses = [item for item in poses if item["id"] == overrides["pose"]]
        pose = choose("pose", poses)
        outfit_tags = set().union(*(
            tags(item) for item in fixed["outfit"]["garments"].values()
        ), set())
        action_tags = available_tags | outfit_tags | tags(pose)
        actions = [
            item for item in self.db["actions"]
            if stage["level"] in item.get("allowed_levels", [stage["level"]])
            and item_allows_intensity(item, intensity)
            and compatible_with_requirements(item, action_tags)
            and hands_required(pose) + hands_required(item) <= 2
        ]
        if stage.get("sfw"):
            actions = [item for item in actions if not (tags(item) & SFW_BLOCKED_DIRECTION_TAGS)]
        if stage["level"] == "explicit":
            actions = [item for item in actions if "explicit_action" in tags(item)]
        elif stage["level"] in {"topless", "nude"}:
            actions = [
                item for item in actions
                if tags(item) & {"erotic_action", "undressing_action"}
            ]
        if stage.get("visual_category") == "dressed_panties_reveal":
            actions = [
                item for item in actions
                if "dressed_panties_reveal_action" in tags(item)
            ]
        else:
            actions = [
                item for item in actions
                if "dressed_panties_reveal_action" not in tags(item)
            ]
        if plateau_kind == "provocative_rear":
            actions = [item for item in actions if "provocative_action" in tags(item)]
        elif plateau_kind == "intimate_closeup":
            actions = [item for item in actions if "closeup_action" in tags(item)]
        elif plateau_kind == "masturbation":
            actions = [item for item in actions if "masturbation_action" in tags(item)]
        elif plateau_kind == "panties_aside":
            actions = [item for item in actions if "panties_aside_action" in tags(item)]
        if recipe and recipe.get("action_tags"):
            required = set(recipe["action_tags"])
            actions = [item for item in actions if required.issubset(tags(item))]
        actions = [
            item for item in actions
            if recipe_focus_compatible(item, recipe, "action")
        ]
        if overrides.get("action"):
            actions = [item for item in actions if item["id"] == overrides["action"]]
        action = choose("action", actions)
        prop = None
        required_prop_tags = set(action.get("requires_prop_tags", []))
        if "prop" in overrides:
            if overrides["prop"]:
                candidates = [
                    item for item in self.db["props"]
                    if item["id"] == overrides["prop"]
                    and not item.get("disabled", False)
                    and compatible_with_requirements(
                        item, available_tags | tags(action)
                    )
                    and (not required_prop_tags or required_prop_tags.issubset(tags(item)))
                    and hands_required(pose) + hands_required(action) + hands_required(item) <= 2
                ]
                if not candidates:
                    raise AppError("Selected prop is incompatible with this shot")
                prop = candidates[0]
            elif required_prop_tags:
                raise AppError("This action requires a compatible prop")
        elif required_prop_tags:
            candidates = [
                item for item in self.db["props"]
                if required_prop_tags.issubset(tags(item))
                and hands_required(pose) + hands_required(action) + hands_required(item) <= 2
            ]
            prop = weighted_choice(self.rng, candidates)
        elif self.rng.random() < 0.18:
            candidates = [
                item for item in self.db["props"]
                if compatible_with_requirements(item, available_tags | tags(action))
                and hands_required(pose) + hands_required(action) + hands_required(item) <= 2
            ]
            if stage["level"] != "explicit":
                casual_props = [
                    item for item in candidates
                    if tags(item) & {"casual_prop", "home_prop"}
                ]
                candidates = casual_props or candidates
            if candidates:
                prop = weighted_choice(self.rng, candidates)
        expression_candidates = list(self.db["expressions"])
        expression_candidates = [
            item for item in expression_candidates
            if item_allows_intensity(item, intensity)
        ]
        required_expression_tags = set(action.get("requires_expression_tags", []))
        if required_expression_tags:
            expression_candidates = [
                item for item in expression_candidates
                if required_expression_tags.issubset(tags(item))
            ]
            if stage["level"] == "lingerie":
                subtle = [
                    item for item in expression_candidates
                    if item["id"] == "expression_shy_sultry"
                ]
                expression_candidates = subtle or expression_candidates
        elif stage["level"] in {"covered", "lingerie"}:
            natural_expressions = {
                "expression_confident", "expression_soft_smile",
                "expression_dreamy", "expression_playful", "expression_serene",
                "expression_shy_sultry",
            }
            expression_candidates = [
                item for item in expression_candidates
                if item["id"] in natural_expressions
            ]
        elif stage["level"] in {"topless", "nude"}:
            expression_candidates = [
                item for item in expression_candidates
                if not tags(item) & {"pleasure_expression", "intense_pleasure_expression"}
            ]
        if overrides.get("expression"):
            expression_candidates = [
                item for item in expression_candidates
                if item["id"] == overrides["expression"]
            ]
        editorial_role = None
        if overrides.get("editorial_role"):
            editorial_role = self.item_index.get(overrides["editorial_role"])
        if editorial_role is None:
            role_hint = "role_" + {
                "covered": "establishing", "lingerie": "development",
                "topless": "reveal", "nude": "nude_study", "explicit": "plateau",
            }.get(stage["level"], "portrait")
            candidates = [item for item in self.db["editorial_roles"] if role_hint in tags(item) or item["id"] == role_hint]
            editorial_role = choose("editorial_role", candidates or self.db["editorial_roles"])
        camera_tags = available_tags | tags(pose) | tags(action) | tags(editorial_role)
        camera = {}
        for key, section in (
            ("shot_size", "shot_sizes"), ("camera_angle", "camera_angles"),
            ("framing", "framings"), ("focus_target", "focus_targets"),
        ):
            candidates = [
                item for item in self.db[section]
                if not item.get("disabled", False)
                and stage["level"] in item.get("allowed_levels", [stage["level"]])
                and item_allows_intensity(item, intensity)
                and compatible_with_requirements(item, camera_tags)
            ]
            wanted = overrides.get(key)
            recipe_options = recipe_reference_ids(recipe, key)
            if wanted:
                candidates = [item for item in candidates if item["id"] == wanted]
            elif recipe_options:
                candidates = [item for item in candidates if item["id"] in recipe_options]
            elif key == "framing" and stage["level"] != "explicit":
                casual_framings = {
                    "framing_centered", "framing_tight_crop",
                    "framing_environmental",
                }
                casual = [
                    item for item in candidates
                    if item["id"] in casual_framings
                ]
                candidates = casual or candidates
            camera[key] = choose(key, candidates)
            camera_tags |= tags(camera[key])
        intimate_arousal_modifier = None
        if recipe and recipe.get("focus_target") == "focus_intimate":
            candidates = [
                item for item in self.db["intimate_arousal_modifiers"]
                if not item.get("disabled", False)
            ]
            intimate_arousal_modifier = choose(
                "intimate_arousal_modifier", candidates
            )
        return {
            "furniture": furniture,
            "location_zone": location_zone,
            **surface_style,
            "pose": pose,
            "action": action,
            "prop": prop,
            "expression": choose("expression", expression_candidates),
            "editorial_role": editorial_role,
            "explicit_recipe": recipe,
            "intimate_arousal_modifier": intimate_arousal_modifier,
            "intensity": intensity,
            **camera,
        }

    def resolve_scene(
        self,
        fixed: dict[str, Any],
        stage: dict[str, Any],
        overrides: dict[str, str] | None = None,
        avoid: dict[str, set[str]] | None = None,
        bag_scope: str | None = None,
    ) -> dict[str, Any]:
        attempts = self.max_scene_attempts
        last_error = "no candidates"
        for _ in range(attempts):
            try:
                scene = dict(fixed)
                scene.update(self.variable_context(
                    stage, fixed, overrides, avoid, bag_scope
                ))
                scene["stage"] = stage
                scene["dependencies"] = self.resolve_dependencies(scene)
                self.validate_scene_rules(scene)
                return scene
            except AppError as exc:
                last_error = str(exc)
        raise AppError(f"Could not resolve a valid scene after {attempts} attempts: {last_error}")

    def scene_items(self, scene: dict[str, Any]) -> list[dict[str, Any]]:
        selected = list(scene["human"].values())
        flattened: list[dict[str, Any]] = []
        for value in selected:
            flattened.extend(value if isinstance(value, list) else [value])
        visible_slots = set(scene["stage"].get("visible_slots", []))
        flattened.extend(
            item for slot, item in scene["outfit"]["garments"].items() if slot in visible_slots
        )
        for modifier_key in ("patterns", "textures"):
            flattened.extend(
                item for slot, item in scene["outfit"].get(modifier_key, {}).items()
                if slot in visible_slots
            )
        flattened.extend([scene[key] for key in ("interior", "furniture", "location_zone", "pose", "action", "expression", "mood", "photography_style", "editorial_role", "shot_size", "camera_angle", "framing", "focus_target")])
        flattened.extend(
            scene[key] for key in ("surface_color", "surface_texture")
            if scene.get(key)
        )
        if scene.get("explicit_recipe"):
            flattened.append(scene["explicit_recipe"])
        if scene.get("intimate_arousal_modifier"):
            flattened.append(scene["intimate_arousal_modifier"])
        if scene.get("prop"):
            flattened.append(scene["prop"])
        flattened.extend(scene.get("dependencies", []))
        return flattened

    def resolve_dependencies(self, scene: dict[str, Any]) -> list[dict[str, Any]]:
        selected = self.scene_items(scene)
        selected_ids = {item["id"] for item in selected}
        dependencies: list[dict[str, Any]] = []
        cursor = 0
        while cursor < len(selected):
            item = selected[cursor]
            cursor += 1
            for required_id in item.get("requires", []):
                if required_id in selected_ids:
                    continue
                dependency = self.item_index.get(required_id)
                if dependency is None or not dependency.get("prompt"):
                    raise AppError(f"Cannot add dependency '{required_id}' required by {item['id']}")
                dependencies.append(dependency)
                selected.append(dependency)
                selected_ids.add(required_id)
        return dependencies

    def validate_scene_rules(self, scene: dict[str, Any]) -> None:
        validate_outfit_color_groups(scene["outfit"])
        self.validate_outfit_environment(scene["outfit"], scene["interior"])
        validate_pose_zone(scene["pose"], scene["location_zone"])
        if not category_allows(scene["interior"], scene["furniture"]):
            raise AppError(
                f"Normal environment {scene['interior']['id']} cannot use luxury "
                f"surface {scene['furniture']['id']}"
            )
        flattened = self.scene_items(scene)
        ids = {item["id"] for item in flattened}
        all_tags = set().union(*(tags(item) for item in flattened))
        all_tags |= set(scene["stage"].get("body_visibility", [])) | set(scene["stage"].get("visible_slots", [])) | {scene["stage"]["level"]}
        if scene["stage"].get("visual_category"):
            all_tags.add(scene["stage"]["visual_category"])
        hand_total = sum(hands_required(scene.get(key)) for key in ("pose", "action", "prop"))
        if hand_total > 2:
            raise AppError(f"Pose, action and prop require {hand_total} hands")
        if scene.get("intensity") not in allowed_scene_intensities(scene):
            raise AppError(
                f"Intensity {scene.get('intensity')} conflicts with stage "
                f"{scene['stage']['id']} and recipe "
                f"{(scene.get('explicit_recipe') or {}).get('id', 'none')}"
            )
        for item in flattened:
            if not item_allows_intensity(item, scene["intensity"]):
                raise AppError(
                    f"Intensity {scene['intensity']} conflicts with {item['id']}"
                )
            missing = set(item.get("requires", [])) - ids
            excluded = set(item.get("excludes", [])) & ids
            missing_tags = set(item.get("requires_tags", [])) - all_tags
            missing_any_tags = set(item.get("requires_any_tags", []))
            if missing_any_tags & all_tags:
                missing_any_tags.clear()
            excluded_tags = set(item.get("excludes_tags", [])) & all_tags
            if missing or excluded or missing_tags or missing_any_tags or excluded_tags:
                raise AppError(
                    f"Rule conflict for {item['id']}: missing={sorted(missing)}, "
                    f"excluded={sorted(excluded)}, missing_tags={sorted(missing_tags)}, "
                    f"missing_any_tags={sorted(missing_any_tags)}, "
                    f"excluded_tags={sorted(excluded_tags)}"
                )
        required_expression_tags = set(scene["action"].get("requires_expression_tags", []))
        if not required_expression_tags.issubset(tags(scene["expression"])):
            raise AppError(
                f"Expression {scene['expression']['id']} is incompatible with action "
                f"{scene['action']['id']}; required tags: {sorted(required_expression_tags)}"
            )
        recipe = scene.get("explicit_recipe")
        if recipe:
            if not recipe_focus_compatible(scene["pose"], recipe, "pose"):
                raise AppError(
                    f"Pose {scene['pose']['id']} conflicts with recipe focus {recipe['id']}"
                )
            if not recipe_focus_compatible(scene["action"], recipe, "action"):
                raise AppError(
                    f"Action {scene['action']['id']} conflicts with recipe focus {recipe['id']}"
                )
        validate_camera_grammar(scene)


ALWAYS_HUMAN_PARTS = (
    "ethnic_appearance", "skin_tone", "face_shape", "eye_shape", "eye_color",
    "eyebrows", "nose", "lips", "cheekbones", "jawline", "height", "body_frame",
    "waist", "hips", "makeup", "manicure",
)

def human_fragments(
    human: dict[str, Any],
    visibility: set[str],
    custom: dict[str, str],
) -> list[str]:
    pregnant = (
        human.get("body_state", {}).get("id") == "body_state_pregnant"
        or "pregnan" in custom.get("human.body_state", "").casefold()
    )
    fragments = [
        custom.get(f"human.{key}") or human[key].get("prompt", "")
        for key in ALWAYS_HUMAN_PARTS
        if not (pregnant and key == "waist")
    ]
    facial_custom = custom.get("human.facial_accents")
    if facial_custom:
        fragments.append(facial_custom)
    else:
        fragments.extend(
            item["prompt"] for item in human.get("facial_accents", [])
            if item.get("prompt")
        )
    if "nipples" in visibility:
        fragments.extend(
            custom.get(f"human.{key}") or human[key]["prompt"]
            for key in ("areola_size", "areola_color", "nipple_size", "nipple_shape")
        )
    if "pubic_area" in visibility:
        fragments.append(custom.get("human.pubic_hair") or human["pubic_hair"]["prompt"])
    if "genitals" in visibility:
        fragments.append(custom.get("human.genital_appearance") or human["genital_appearance"]["prompt"])
    skin_marking = human.get("skin_marking", {})
    skin_tags = tags(human.get("skin_tone", {}))
    skin_class = next(
        (
            value for value in ("light_skin", "medium_skin", "dark_skin")
            if value in skin_tags
        ),
        None,
    )

    def marking_prompt(field: str) -> str:
        variants = skin_marking.get(f"{field}_by_skin", {})
        return variants.get(skin_class, skin_marking.get(field, ""))

    custom_skin_marking = custom.get("human.skin_marking")
    if custom_skin_marking and visibility & {"nipples", "pubic_area", "genitals"}:
        # A local Director override replaces the catalog tan-line behavior,
        # including its skin-aware variants, instead of being silently stored.
        fragments.append(custom_skin_marking)
    else:
        if "nipples" in visibility and skin_marking.get("bra_line_prompt"):
            fragments.append(marking_prompt("bra_line_prompt"))
        if visibility & {"pubic_area", "genitals"} and skin_marking.get("panty_line_prompt"):
            fragments.append(marking_prompt("panty_line_prompt"))
    return [fragment for fragment in fragments if fragment]


def compile_scene(
    db: dict[str, Any], scene: dict[str, Any], include_age: bool = True
) -> tuple[str, str, list[str]]:
    defaults = db["prompt_defaults"]
    custom = scene.get("custom_values", {})
    stage = scene["stage"]
    stage_visibility = set(stage.get("body_visibility", []))
    visible_slots = set(stage.get("visible_slots", []))
    outfit = scene["outfit"]
    visible_chest_layers = [
        outfit["garments"][slot]
        for slot in CHEST_GARMENT_SLOTS
        if slot in visible_slots and slot in outfit["garments"]
    ]
    layered_sheer_outer = any(
        slot in visible_slots
        and slot in outfit["garments"]
        and tags(outfit["garments"][slot]) & {"sheer", "transparent"}
        for slot in ("upperwear", "full_body", "outerwear")
    )
    if layered_sheer_outer and "bra" in outfit["garments"]:
        visible_chest_layers.append(outfit["garments"]["bra"])
    opaque_chest_layers = [
        item for item in visible_chest_layers
        if not (tags(item) & OPAQUE_LINGERIE_BLOCKED_TAGS)
    ]
    sheer_chest_layers = [
        item for item in visible_chest_layers
        if tags(item) & {"sheer", "transparent"}
    ]
    if opaque_chest_layers:
        chest_coverage = "opaque"
    elif sheer_chest_layers:
        chest_coverage = "sheer"
    elif any("open_cup" in tags(item) for item in visible_chest_layers):
        chest_coverage = "open"
    else:
        chest_coverage = "none"
    covered_chest = (
        chest_coverage in {"opaque", "sheer"}
        or not bool({"breasts", "nipples"} & stage_visibility)
    )
    visibility = set(stage_visibility)
    recipe = scene.get("explicit_recipe")
    plateau_kind = stage.get("plateau_kind") or (
        recipe.get("plateau_kind") if recipe else None
    )
    if plateau_kind == "provocative_rear":
        visibility -= {"breasts", "nipples"}
    if covered_chest:
        visibility -= {"breasts", "nipples"}
    xxx_prompt = defaults.get("xxx_plateau_prompts", {}).get(plateau_kind, "")
    age_prompt = (
        custom.get("human.age") or scene["human"]["age"]["prompt"]
        if include_age else
        "adult woman"
    )
    positive_prefix = defaults.get("positive_prefix", "").replace("{age}", age_prompt)
    fragments = [positive_prefix]
    human = scene["human"]
    breast_size_prompt = (
        human["breast_size"]["covered_prompt"]
        if covered_chest else
        custom.get("human.breast_size") or human["breast_size"]["prompt"]
    )
    breast_shape_prompt = (
        human["breast_shape"]["covered_prompt"]
        if covered_chest else
        custom.get("human.breast_shape") or human["breast_shape"]["prompt"]
    )
    hair_parts = [
        custom.get(f"human.{key}") or human[key]["prompt"]
        for key in ("hair_texture", "hair_length", "hair_style", "hair_color")
    ]
    anatomy_identity = f"{breast_size_prompt} with {breast_shape_prompt}"
    fragments.append(
        f"subject has {anatomy_identity}; subject has {' '.join(hair_parts)}"
    )
    body_state_prompt = (
        custom.get("human.body_state")
        or scene["human"].get("body_state", {}).get("prompt", "")
    )
    if body_state_prompt:
        # Structural body modifiers need attention before stage, styling and
        # fine identity details. The neutral default deliberately emits nothing.
        fragments.append(body_state_prompt)
    scene_pools = db["settings"]["scene_defaults"].get("pools", {})
    casual_photo_ids = set(scene_pools.get("photography_styles", [])) | set(
        scene_pools.get("explicit_photography_styles", [])
    )
    casual_role_prompts = {
        "role_establishing": "casual opening snapshot showing the subject in her room",
        "role_portrait": "relaxed personal portrait snapshot",
        "role_development": "natural mid-sequence home snapshot",
        "role_reveal": "candid reveal moment",
        "role_nude_study": "relaxed natural nude snapshot",
        "role_plateau": "close candid explicit home snapshot",
        "role_peak": "intense closing snapshot",
    }

    def shot_prompt(key: str, item: dict[str, Any]) -> str:
        override = custom.get(f"shot.{key}")
        if override:
            return override
        if (
            key == "editorial_role"
            and scene.get("photography_style", {}).get("id") in casual_photo_ids
        ):
            return casual_role_prompts.get(item["id"], item["prompt"])
        return item["prompt"]
    wardrobe_color_parts = [
        f"the {slot.replace('_', ' ')} layer is exactly "
        f"{custom.get(f'outfit.colors.{slot}') or outfit['colors'][slot]['prompt']}"
        for slot in outfit["template"]["slots"]
        if slot in visible_slots and slot in outfit["garments"]
    ]
    if wardrobe_color_parts:
        fragments.append(
            "fixed wardrobe colors for this frame: " + "; ".join(wardrobe_color_parts)
        )
    visible_outer_chest = [
        outfit["garments"][slot]
        for slot in ("upperwear", "full_body", "outerwear")
        if slot in visible_slots and slot in outfit["garments"]
    ]
    layered_sheer_chest = any(
        tags(item) & {"sheer", "transparent"} for item in visible_outer_chest
    )
    garment_states = scene.get("garment_states", {})
    open_upperwear = garment_states.get("upperwear") == "unbuttoned_open"
    covered_sheer = stage.get("level") == "covered" and layered_sheer_chest
    lingerie_sheer = stage.get("level") == "lingerie" and chest_coverage == "sheer"
    # A stage label alone is UI metadata. These anchors state the visual contract
    # explicitly in vocabulary image models reliably understand.
    stage_anchors = {
        "covered": (
            "button-front shirt worn fully unbuttoned and open at both sides, the intact "
            "bra underneath remains the complete chest-covering layer"
            if open_upperwear else
            "(fully opaque fitted lining beneath the translucent outer garment:1.5), "
            "uninterrupted lining fabric across the entire chest, uniform lining color "
            "and weave, clean smooth garment surface"
            if covered_sheer else
            "(fully opaque upper-body garment:1.5), uninterrupted fabric across the "
            "entire chest, uniform garment color and weave, clean smooth garment surface"
        ),
        "lingerie": (
            (
                "one sheer upper-body garment worn as the outer layer over one coordinated "
                "distinct bra underneath, the bra entirely beneath the outer garment, "
                "clean physically correct clothing layers"
                if layered_sheer_chest else
                "one fully opaque upper-body garment as the visible chest layer, clean "
                "uninterrupted garment surface"
            )
            if visible_outer_chest else
            (
                "(one intact sheer chest-covering lingerie garment:1.5), translucent "
                "fabric continuously covers the entire chest, fabric remains visibly "
                "between skin and camera, smooth realistic tension without openings or protrusions"
                if chest_coverage == "sheer" else
                (
                    "(one fully opaque chest-covering lingerie garment:1.5), opaque cups "
                    "form complete continuous uninterrupted fabric coverage, smooth low-relief cup surface, "
                    "all body contours fully contained beneath the garment, clean smooth garment surface"
                    if covered_chest else
                    "open-cup lingerie composition with exposed chest anatomy framed by "
                    "the garment construction"
                )
            )
        ),
        "topless": "topless, bare breasts and visible nipples, lower-body garments visible",
        "nude": "fully nude body, bare breasts, visible nipples, pubic area and genitals visible",
        "explicit": "explicit adult pose, bare breasts, visible nipples, pubic area and genitals visible",
    }
    stage_anchor = (
        db["settings"]["dressed_panties_reveal"]["positive_prompt"]
        if stage.get("visual_category") == "dressed_panties_reveal"
        else stage_anchors.get(stage.get("level"), "")
    )
    # Explicit compiler priority: subject -> camera/direction -> anatomy ->
    # traits/garments -> location/treatment. Keep the order data-declared and
    # stable because diffusion models give earlier structural tokens more weight.
    stage_custom = custom.get("shot.stage")
    if stage_custom:
        fragments.append(stage_custom)
    if scene.get("explicit_recipe"):
        fragments.append(
            custom.get("shot.explicit_recipe") or scene["explicit_recipe"]["prompt"]
        )
    for key in ("pose", "action", "editorial_role", "shot_size", "camera_angle", "framing", "focus_target"):
        item = scene.get(key)
        if item:
            fragments.append(
                shot_prompt(key, item)
                if key in {"editorial_role", "shot_size", "camera_angle", "framing", "focus_target"}
                else custom.get(f"shot.{key}") or item["prompt"]
            )

    # A recipe can establish a stricter anatomical orientation than a generic
    # explicit stage. Avoid frontal anatomy wording for rear views.
    if xxx_prompt:
        fragments.append(xxx_prompt)
    if stage_anchor and plateau_kind != "provocative_rear":
        fragments.append(stage_anchor)
    if scene.get("intensity"):
        fragments.append(
            custom.get("shot.intensity") or f"{scene['intensity']} visual intensity"
        )
    fragments.extend(
        human_fragments(scene["human"], visibility, custom)
    )
    for contract in db["settings"]["wardrobe_compatibility"]["render_contracts"]:
        slot = contract["slot"]
        garment = outfit["garments"].get(slot)
        if (
            garment
            and slot in visible_slots
            and tags(garment) & set(contract["garment_tags_any"])
            and stage_visibility & set(contract["body_visibility_any"])
        ):
            fragments.append(contract["positive_prompt"])
    if scene.get("intimate_arousal_modifier"):
        fragments.append(scene["intimate_arousal_modifier"]["prompt"])
    if custom.get("outfit.template"):
        fragments.append(custom["outfit.template"])
    reveals_cameltoe = False
    for slot in outfit["template"]["slots"]:
        if slot in visible_slots and slot in outfit["garments"]:
            garment = outfit["garments"][slot]
            garment_parts = [custom.get(f"outfit.colors.{slot}") or outfit["colors"][slot]["prompt"]]
            if custom.get(f"outfit.patterns.{slot}") or slot in outfit.get("patterns", {}):
                garment_parts.append(custom.get(f"outfit.patterns.{slot}") or outfit["patterns"][slot]["prompt"])
            if custom.get(f"outfit.textures.{slot}") or slot in outfit.get("textures", {}):
                garment_parts.append(custom.get(f"outfit.textures.{slot}") or outfit["textures"][slot]["prompt"])
            garment_parts.append(custom.get(f"outfit.garments.{slot}") or garment["prompt"])
            state = garment_states.get(slot)
            if state == "unbuttoned_open":
                garment_parts.append(
                    "worn fully unbuttoned with both front panels hanging open"
                )
            elif state == "lowered_to_hips":
                garment_parts.append(
                    "unfastened and lowered to the hips, still visibly worn around the hips"
                )
            garment_fragment = " ".join(garment_parts)
            if slot == "bra":
                garment_fragment = (
                    f"one single-layer {garment_fragment}, straps and underband matching "
                    "the same base color and material"
                )
            fragments.append(garment_fragment)
            reveals_cameltoe = reveals_cameltoe or garment.get("reveals_cameltoe", False)
    if layered_sheer_chest and "bra" not in visible_slots:
        bra = outfit["garments"].get("bra")
        if bra:
            bra_parts = [
                custom.get("outfit.colors.bra") or outfit["colors"]["bra"]["prompt"]
            ]
            if custom.get("outfit.patterns.bra") or "bra" in outfit.get("patterns", {}):
                bra_parts.append(
                    custom.get("outfit.patterns.bra") or outfit["patterns"]["bra"]["prompt"]
                )
            if custom.get("outfit.textures.bra") or "bra" in outfit.get("textures", {}):
                bra_parts.append(
                    custom.get("outfit.textures.bra") or outfit["textures"]["bra"]["prompt"]
                )
            bra_parts.append(custom.get("outfit.garments.bra") or bra["prompt"])
            fragments.append(
                "the same underlying " + " ".join(bra_parts) + " clearly visible beneath the sheer outer garment"
            )
    panties = outfit["garments"].get("panties") if "panties" in visible_slots else None
    legwear = outfit["garments"].get("legwear") if "legwear" in visible_slots else None
    lowerwear = outfit["garments"].get("lowerwear") if "lowerwear" in visible_slots else None
    legwear_prompt = (legwear or {}).get("prompt", "").casefold()
    layered_hosiery = bool(
        panties
        and legwear
        and (
            "pantyhose" in tags(legwear)
            or "pantyhose" in legwear_prompt
            or "tights" in legwear_prompt
        )
    )
    if layered_hosiery:
        fragments.append(
            "the panties are worn underneath the hosiery garment, hosiery forms the "
            "continuous outer layer over the panties"
        )
    if legwear:
        support_contracts = {
            "waist_continuous": (
                "one continuous waist-supported hosiery garment, with uninterrupted "
                "fabric running from its fitted waistband down both legs"
            ),
            "self_supporting": (
                "two separate thigh-high stockings, each held in place solely by its "
                "own fitted integrated stay-up band"
            ),
            "garter_required": (
                "two separate stockings physically fastened to the visible waist garter "
                "belt by four straight aligned support straps"
            ),
        }
        if legwear.get("support_mode") in support_contracts:
            fragments.append(support_contracts[legwear["support_mode"]])
    if lowerwear_covers_legs(lowerwear) and legwear:
        fragments.append(
            "trouser fabric forms the continuous outer layer from waist to ankle hems, "
            "ankle socks begin below the trouser hems"
        )
    if reveals_cameltoe:
        fragments.append(defaults["cameltoe_prompt"])

    # Location and its physical surface are also persistent set identity.
    for key in ("location_zone", "interior", "furniture", "mood", "photography_style"):
        item = scene.get(key)
        if item:
            director_key = f"shot.{key}" if key == "furniture" else f"scene.{key}"
            if key == "furniture":
                base = custom.get(director_key) or item["prompt"]
                modifier_parts = []
                for kind in ("color", "texture"):
                    modifier = scene.get(f"surface_{kind}")
                    modifier_prompt = custom.get(f"shot.surface_{kind}") or (
                        modifier.get("prompt", "") if modifier else ""
                    )
                    if modifier_prompt:
                        modifier_parts.append(modifier_prompt)
                target = item.get("surface_texture_target") or item.get(
                    "surface_color_target", "surface"
                )
                fragments.append(
                    f"{base}, {' '.join(modifier_parts)} {target}"
                    if modifier_parts else base
                )
            else:
                fragments.append(custom.get(director_key) or item["prompt"])

    # Remaining transient detail does not override structural direction.
    if scene.get("garment_transition") and plateau_kind != "provocative_rear":
        fragments.append(
            custom.get("shot.garment_transition")
            or scene["garment_transition"]["prompt"]
        )
    for key in ("prop", "expression"):
        item = scene.get(key)
        if item:
            if key == "expression" and plateau_kind == "provocative_rear":
                fragments.append(
                    "face turned mostly away from the camera, facial features not a composition focus"
                )
            else:
                fragments.append(custom.get(f"shot.{key}") or item["prompt"])
        elif key == "prop" and custom.get("shot.prop"):
            fragments.append(custom["shot.prop"])
    fragments.extend(item["prompt"] for item in scene.get("dependencies", []))
    fragments.append(defaults.get("positive_suffix", ""))
    unique_fragments = []
    seen_fragments = set()
    for fragment in fragments:
        clean = fragment.strip(" ,")
        normalized = re.sub(r"\s+", " ", clean.casefold())
        if clean and normalized not in seen_fragments:
            unique_fragments.append(clean)
            seen_fragments.add(normalized)
    positive = ", ".join(unique_fragments)
    negative = defaults.get("negative_prompt", "")
    negative_profiles = defaults["negative_profiles"]
    if covered_chest and not lingerie_sheer:
        negative = f"{negative}, {negative_profiles['covered_opaque']}"
    if layered_hosiery:
        negative = f"{negative}, {negative_profiles['layered_hosiery']}"
    if plateau_kind:
        negative = f"{negative}, {negative_profiles['explicit']}"
    kind_negative = negative_profiles.get("explicit_by_recipe", {}).get(
        plateau_kind, ""
    )
    if kind_negative:
        negative = f"{negative}, {kind_negative}"
    ids = []
    for value in scene["human"].values():
        ids.extend(item["id"] for item in value) if isinstance(value, list) else ids.append(value["id"])
    ids.extend(item["id"] for slot, item in outfit["garments"].items() if slot in visible_slots)
    if layered_sheer_chest and "bra" in outfit["garments"]:
        ids.append(outfit["garments"]["bra"]["id"])
    for modifier_key in ("patterns", "textures"):
        ids.extend(
            item["id"] for slot, item in outfit.get(modifier_key, {}).items()
            if slot in visible_slots
        )
        if layered_sheer_chest and "bra" in outfit.get(modifier_key, {}):
            ids.append(outfit[modifier_key]["bra"]["id"])
    ids.extend(scene[key]["id"] for key in ("pose", "action", "expression", "interior", "furniture", "location_zone", "mood", "photography_style", "editorial_role", "shot_size", "camera_angle", "framing", "focus_target"))
    ids.extend(
        scene[key]["id"] for key in ("surface_color", "surface_texture")
        if scene.get(key)
    )
    if scene.get("explicit_recipe"):
        ids.append(scene["explicit_recipe"]["id"])
    if scene.get("prop"):
        ids.append(scene["prop"]["id"])
    ids.extend(item["id"] for item in scene.get("dependencies", []))
    return positive, negative, list(dict.fromkeys(ids))


def prompt_lint(scene: dict[str, Any], positive: str) -> list[str]:
    warnings: list[str] = []
    folded = positive.casefold()
    stage = scene["stage"]
    action_context_tags = (
        set(stage.get("body_visibility", []))
        | set(stage.get("visible_slots", []))
        | {stage["level"]}
        | tags(scene.get("interior", {}))
        | tags(scene.get("furniture", {}))
        | tags(scene.get("pose", {}))
        | set().union(*(
            tags(item) for item in scene["outfit"]["garments"].values()
        ), set())
    )
    if stage.get("visual_category"):
        action_context_tags.add(stage["visual_category"])
    action = scene.get("action") or {}
    missing_action_tags = set(action.get("requires_tags", [])) - action_context_tags
    if missing_action_tags:
        warnings.append(
            "Action requires unavailable tags: "
            + ", ".join(sorted(missing_action_tags))
        )
    required_any = set(action.get("requires_any_tags", []))
    if required_any and not required_any & action_context_tags:
        warnings.append(
            "Action requires one of unavailable tags: "
            + ", ".join(sorted(required_any))
        )
    excluded_action_tags = set(action.get("excludes_tags", [])) & action_context_tags
    if excluded_action_tags:
        warnings.append(
            "Action excludes present tags: "
            + ", ".join(sorted(excluded_action_tags))
        )
    if folded.count("single subject") > 1:
        warnings.append("Subject identity is repeated")
    stale_sequence_phrases = (
        "consistent face and body in every frame",
        "the same subject in every frame",
    )
    if any(phrase in folded for phrase in stale_sequence_phrases):
        warnings.append("Prompt claims cross-frame model memory")
    if scene["stage"]["level"] == "covered" and any(term in folded for term in ("fully nude", "exposed genitals")):
        warnings.append("Covered stage contains exposed-content wording")
    coverage_anchors = {"covered": "fully opaque"}
    if (
        scene["stage"]["level"] == "lingerie"
        and not ({"breasts", "nipples"} & set(
            scene["stage"].get("body_visibility", [])
        ))
    ):
        coverage_anchors["lingerie"] = "one fully opaque chest-covering lingerie garment"
    expected_anchor = coverage_anchors.get(scene["stage"]["level"])
    if expected_anchor and expected_anchor not in folded:
        warnings.append("Clothing coverage contract is missing")
    for slot in scene["stage"].get("visible_slots", []):
        garment = scene["outfit"]["garments"].get(slot)
        garment_prompt = scene.get("custom_values", {}).get(
            f"outfit.garments.{slot}"
        ) or (garment or {}).get("prompt", "")
        if garment_prompt and garment_prompt.casefold() not in folded:
            warnings.append(f"Visible garment is missing from prompt: {slot}")
    if scene.get("focus_target", {}).get("id") == "environment" and scene.get("shot_size", {}).get("id") in {"intimate_macro", "breast_closeup"}:
        warnings.append("Environmental focus conflicts with close-up framing")
    word_count = len(positive.split())
    if word_count > 500:
        warnings.append(
            f"Long prompt ({word_count} words); review it without automatic truncation"
        )
    return warnings


def model_signature(human: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in human.items():
        if isinstance(value, list):
            parts.extend(item["id"] for item in value)
        else:
            parts.append(value["id"])
    return "+".join(parts)


def model_description(
    human: dict[str, Any], custom: dict[str, str] | None = None
) -> str:
    custom = custom or {}
    keys = (
        "age", "ethnic_appearance", "skin_tone", "hair_length", "hair_style",
        "hair_color", "height", "body_frame", "body_state", "breast_size",
    )
    parts = [custom.get(f"human.{key}") or human[key]["prompt"] for key in keys]
    summarized = {f"human.{key}" for key in keys}
    parts.extend(
        value for key, value in custom.items()
        if key.startswith("human.") and key not in summarized and value
    )
    return " · ".join(part for part in parts if part)


def photoshoot_signature(context: dict[str, Any]) -> tuple[Any, ...]:
    outfit = context["outfit"]
    garments = tuple(
        (
            slot,
            item["id"],
            outfit["colors"][slot]["id"],
            outfit.get("patterns", {}).get(slot, {}).get("id"),
            outfit.get("textures", {}).get(slot, {}).get("id"),
        )
        for slot, item in sorted(outfit["garments"].items())
    )
    return (
        model_signature(context["human"]),
        outfit["template"]["id"],
        garments,
        context["interior"]["id"],
        context["furniture"]["id"],
        context["mood"]["id"],
        context["photography_style"]["id"],
    )


def stage_for_index(
    template: dict[str, Any],
    index: int,
    count: int,
    mode: str,
    rng: random.Random,
    nsfw_percent: float,
    plateau_percent: float,
) -> dict[str, Any]:
    stages = template["stages"]
    if mode == "photoshoot":
        effective = effective_photoshoot_stages(template)
        safe = [stage for stage in effective if stage["level"] not in NSFW_LEVELS]
        nsfw = [stage for stage in effective if stage["level"] in NSFW_LEVELS]
        nsfw_count = min(count, math.ceil(count * nsfw_percent / 100)) if nsfw_percent > 0 else 0
        plateau_count = min(nsfw_count, math.ceil(count * plateau_percent / 100)) if plateau_percent > 0 else 0
        safe_count = count - nsfw_count
        if index < safe_count:
            return safe[min(len(safe) - 1, index * len(safe) // safe_count)]
        nsfw_index = index - safe_count
        transition_count = nsfw_count - plateau_count
        if nsfw_index < transition_count:
            transition_stages = nsfw if plateau_count == 0 else [
                stage for stage in nsfw if stage["level"] != "explicit"
            ]
            return progressive_stage(transition_stages, nsfw_index, transition_count)
        explicit_stage = next(stage for stage in nsfw if stage["level"] == "explicit")
        plateau_kinds = [
            {"plateau_kind": "provocative_rear"},
            {"plateau_kind": "intimate_closeup"},
            {"plateau_kind": "masturbation"},
        ]
        plateau_index = nsfw_index - transition_count
        kind = progressive_stage(plateau_kinds, plateau_index, plateau_count)["plateau_kind"]
        if kind == "intimate_closeup" and "panties" in template.get("slots", {}) and rng.random() < 0.5:
            kind = "panties_aside"
        result = copy.deepcopy(explicit_stage)
        result["id"] = f"{explicit_stage['id']}_{kind}"
        result["plateau_kind"] = kind
        result["visible_slots"] = (
            [slot for slot in ("panties", "legwear", "footwear", "accessories") if slot in template.get("slots", {})]
            if kind == "panties_aside" else []
        )
        result["body_visibility"] = ["breasts", "nipples", "pubic_area", "genitals"]
        return result
    return weighted_choice(rng, stages)


def maybe_dressed_panties_reveal(
    db: dict[str, Any],
    stage: dict[str, Any],
    outfit: dict[str, Any],
    rng: random.Random,
) -> dict[str, Any]:
    """Turn a compatible dressed frame into a probabilistic panties reveal."""
    rule = db["settings"]["dressed_panties_reveal"]
    if stage["level"] != "covered" or "panties" not in outfit["garments"]:
        return stage
    compatible_ids = set(rule["compatible_outer_ids"])
    if not any(
        outfit["garments"].get(slot, {}).get("id") in compatible_ids
        and slot in stage.get("visible_slots", [])
        for slot in rule["outer_slots"]
    ):
        return stage
    if rng.random() >= rule["chance"]:
        return stage
    return dressed_panties_reveal_stage(stage, outfit)


def dressed_panties_reveal_stage(
    stage: dict[str, Any], outfit: dict[str, Any]
) -> dict[str, Any]:
    """Author the deterministic lifted-hem state after compatibility is known."""
    result = copy.deepcopy(stage)
    result["id"] = f"{stage['id']}_dressed_panties_reveal"
    result["visual_category"] = "dressed_panties_reveal"
    result.pop("sfw", None)
    result["visible_slots"] = list(dict.fromkeys([
        *stage.get("visible_slots", []), "panties",
    ]))
    result["body_visibility"] = []
    return result


def weighted_shuffle_sequence(
    rng: random.Random, candidates: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """Return deterministic weighted bags, exhausted before any candidate repeats."""
    if count <= 0:
        return []
    if not candidates:
        raise AppError("Cannot build a shuffle bag without candidates")
    result: list[dict[str, Any]] = []
    while len(result) < count:
        remaining = list(candidates)
        bag: list[dict[str, Any]] = []
        while remaining:
            selected = weighted_choice(rng, remaining)
            bag.append(selected)
            remaining.remove(selected)
        if result and len(bag) > 1 and bag[0]["id"] == result[-1]["id"]:
            bag[0], bag[1] = bag[1], bag[0]
        result.extend(bag)
    return result[:count]


def full_xxx_recipe_plan(
    db: dict[str, Any], count: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Plan an explicit editorial arc over concrete enabled recipe records."""
    recipes = [
        item for item in db["explicit_recipes"]
        if not item.get("disabled", False)
    ]
    plateau = [item for item in recipes if item.get("intensity") != "peak"]
    peak = [item for item in recipes if item.get("intensity") == "peak"]
    if not peak:
        raise AppError("Full XXX planning requires at least one enabled peak recipe")
    if count == 1:
        return weighted_shuffle_sequence(rng, peak, 1)
    if not plateau:
        raise AppError("Full XXX planning requires at least one enabled explicit recipe")
    return (
        weighted_shuffle_sequence(rng, plateau, count - 1)
        + weighted_shuffle_sequence(rng, peak, 1)
    )


def full_xxx_stage(
    template: dict[str, Any], recipe: dict[str, Any], index: int
) -> dict[str, Any]:
    """Build an explicit stage bound to one preplanned concrete recipe."""
    effective = effective_photoshoot_stages(template)
    explicit = next(stage for stage in effective if stage["level"] == "explicit")
    result = copy.deepcopy(explicit)
    result["id"] = f"{explicit['id']}_full_xxx_{index + 1}_{recipe['id']}"
    result["plateau_kind"] = recipe["plateau_kind"]
    result["planned_recipe_id"] = recipe["id"]
    result["planned_intensity"] = recipe.get("intensity", "explicit")
    result["visible_slots"] = []
    result["body_visibility"] = ["breasts", "nipples", "pubic_area", "genitals"]
    return result


def sfw_stage(
    template: dict[str, Any], index: int, count: int, mode: str, rng: random.Random
) -> dict[str, Any]:
    """Select only stages that guarantee covered breasts and genitals."""
    stages = [stage for stage in effective_photoshoot_stages(template) if is_sfw_stage(stage)]
    if not stages:
        raise AppError(f"Outfit template {template['id']} has no SFW-compatible covered stage")
    if mode == "photoshoot":
        result = copy.deepcopy(stages[min(len(stages) - 1, index * len(stages) // count)])
    else:
        result = copy.deepcopy(weighted_choice(rng, stages))
    result["sfw"] = True
    return result


def require_requests() -> Any:
    if requests is None:
        raise AppError("The 'requests' package is required for this command. Install it with: python3 -m pip install --user requests")
    return requests


def comfy_session(db: dict[str, Any]) -> tuple[Any, str, float]:
    module = require_requests()
    session = module.Session()
    config, _ = load_config()
    url = config["comfy"]["url"].rstrip("/")
    timeout = float(config["comfy"]["http_timeout_seconds"])
    return session, url, timeout


def comfy_history_from_valhalla(item: dict[str, Any]) -> bool:
    prompt_record = item.get("prompt", [])
    if len(prompt_record) < 4 or not isinstance(prompt_record[3], dict):
        return False
    metadata = prompt_record[3]
    return (
        metadata.get("valhalla_origin") is True
        or str(metadata.get("client_id", "")).startswith("valhalla-")
    )


def latest_comfy_workflow(
    db: dict[str, Any], media_type: str = "image"
) -> tuple[str, dict[str, Any]]:
    media_type = validate_media_type(media_type)
    session, url, timeout = comfy_session(db)
    try:
        response = session.get(f"{url}/history", timeout=timeout)
        response.raise_for_status()
        history = response.json()
    except Exception as exc:
        raise AppError(f"Could not fetch ComfyUI history: {exc}") from exc
    completed = []
    for prompt_id, item in history.items():
        status = item.get("status", {})
        if (
            status.get("completed") and status.get("status_str") == "success"
            and item.get("outputs") and not comfy_history_from_valhalla(item)
        ):
            workflow = item.get("prompt", [None, None, {}])[2]
            if media_type == "video" and not workflow_is_video(workflow):
                continue
            if media_type == "image" and workflow_is_video(workflow):
                continue
            timestamp = 0
            for message, payload in status.get("messages", []):
                if message == "execution_success":
                    timestamp = payload.get("timestamp", timestamp)
            completed.append((timestamp, prompt_id, item))
    if not completed:
        raise AppError(
            "ComfyUI history contains no successful external workflow with outputs. "
            "Run the desired workflow directly in ComfyUI first"
        )
    _, prompt_id, item = max(completed, key=lambda entry: entry[0])
    prompt_record = item.get("prompt", [])
    if len(prompt_record) < 3 or not isinstance(prompt_record[2], dict):
        raise AppError(f"History item {prompt_id} does not contain an API workflow in prompt[2]")
    return prompt_id, prompt_record[2]


def workflow_profile_directory(
    db: dict[str, Any], db_path: Path, media_type: str = "image"
) -> Path:
    validate_media_type(media_type)
    config, path = load_config()
    root = resolve_path(path.parent, config["comfy"]["workflows_dir"])
    migrate_legacy_video_workflows(root)
    return root


def migrate_legacy_video_workflows(root: Path) -> None:
    """Move profiles from the old workflows/video directory into workflows."""
    legacy = root / "video"
    if not legacy.is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    for source in sorted(legacy.iterdir()):
        if not source.is_file():
            continue
        target = root / source.name
        if target.exists():
            if target.is_file() and target.read_bytes() == source.read_bytes():
                source.unlink()
                continue
            raise AppError(
                f"Cannot migrate video workflow {source.name}: "
                f"a different file already exists in {root}"
            )
        source.replace(target)
    try:
        legacy.rmdir()
    except OSError:
        pass


def workflow_profile_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise AppError("Profile name must contain letters or numbers")
    return slug[:80]


def workflow_model_name(workflow: dict[str, Any]) -> str:
    keys = ("ckpt_name", "unet_name", "model_name", "checkpoint", "model")
    for node in workflow.values():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        for key in keys:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).name.rsplit(".", 1)[0].replace("_", " ").strip()
    return "ComfyUI model"


def media_workflow_settings(config: dict[str, Any], media_type: str = "image") -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    configured = config["comfy"].get("media_profiles", {}).get(media_type)
    if isinstance(configured, dict):
        return dict(configured)
    # Existing installations keep their image settings at the old locations.
    if media_type == "image":
        return {
            "source": config["comfy"].get("workflow_source", "profiles"),
            **dict(config["comfy"]["profiles"]),
        }
    return {"source": "profiles", "production": None}


def load_workflow_profile_registry(
    db: dict[str, Any], db_path: Path, media_type: str = "image"
) -> dict[str, Any]:
    config, _ = load_config()
    settings = media_workflow_settings(config, media_type)
    return {key: settings.get(key) for key in ("production", "preview") if key in settings}


def workflow_source(media_type: str = "image") -> str:
    config, _ = load_config()
    return str(media_workflow_settings(config, media_type).get("source", "profiles"))


def save_workflow_profile_registry(
    db: dict[str, Any], db_path: Path, registry: dict[str, Any], source: str | None = None,
    media_type: str = "image",
) -> None:
    media_type = validate_media_type(media_type)
    config, _ = load_config()
    legacy_config = "media_profiles" not in config["comfy"]
    if legacy_config:
        # Migrate legacy image settings on the first write while also creating
        # the independent video registry.  Keep the old aliases for clients
        # and exports that still read them.
        config["comfy"]["media_profiles"] = {
            "image": {
                "source": config["comfy"].get("workflow_source", "profiles"),
                "production": config["comfy"]["profiles"].get("production"),
                "preview": config["comfy"]["profiles"].get("preview"),
            },
            "video": {"source": "profiles", "production": None},
        }
    if media_type == "image" and legacy_config:
        config["comfy"]["profiles"] = {
            "production": registry.get("production"),
            "preview": registry.get("preview"),
        }
        config["comfy"]["media_profiles"]["image"]["production"] = registry.get("production")
        config["comfy"]["media_profiles"]["image"]["preview"] = registry.get("preview")
        if source is not None:
            if source not in {"profiles", "live"}:
                raise AppError("Workflow source must be profiles or live")
            config["comfy"]["workflow_source"] = source
            config["comfy"]["media_profiles"]["image"]["source"] = source
    else:
        media = dict(config["comfy"].setdefault("media_profiles", {}).get(media_type, {}))
        media["production"] = registry.get("production")
        if media_type == "image":
            media["preview"] = registry.get("preview")
        if source is not None:
            if source not in {"profiles", "live"}:
                raise AppError("Workflow source must be profiles or live")
            media["source"] = source
        config["comfy"]["media_profiles"][media_type] = media
    save_config(config)


def list_workflow_profiles(
    db: dict[str, Any], db_path: Path, media_type: str = "image"
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    directory = workflow_profile_directory(db, db_path, media_type)
    registry = load_workflow_profile_registry(db, db_path, media_type)
    profiles = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.workflow.json")):
            profile_id = path.name.removesuffix(".workflow.json")
            try:
                workflow = json.loads(path.read_text(encoding="utf-8"))
                if workflow_is_video(workflow) != (media_type == "video"):
                    continue
                mapping = (
                    detect_video_node_mapping(workflow)
                    if media_type == "video" else detect_node_mapping(workflow, include_fast=True)
                )
                valid, error = True, None
                negative_conditioning = mapping.get("negative_prompt") is not None if media_type == "image" else True
            except Exception as exc:
                valid, error = False, str(exc)
                negative_conditioning = False
            profiles.append({
                "id": profile_id,
                "name": profile_id.replace("-", " ").title(),
                "file": path.name,
                "valid": valid,
                "error": error,
                "negative_conditioning": negative_conditioning,
            })
    ids = {item["id"] for item in profiles if item["valid"]}
    for mode in (("production", "preview") if media_type == "image" else ("production",)):
        if registry.get(mode) not in ids:
            registry[mode] = None
    return {
        "profiles": profiles,
        "production": registry.get("production"),
        "preview": registry.get("preview"),
        "source": workflow_source(media_type),
        "media_type": media_type,
    }


def workflow_capture_candidate(
    db: dict[str, Any], media_type: str = "image"
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    prompt_id, workflow = latest_comfy_workflow(db, media_type)
    if media_type == "video":
        detect_video_node_mapping(workflow)
    else:
        detect_node_mapping(workflow, include_fast=True)
    name = workflow_model_name(workflow)
    return {"prompt_id": prompt_id, "suggested_name": name, "suggested_id": workflow_profile_slug(name)}


def capture_workflow_profile(
    db: dict[str, Any], db_path: Path, name: str, replace: bool,
    media_type: str = "image",
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    prompt_id, workflow = latest_comfy_workflow(db, media_type)
    mapping = (
        detect_video_node_mapping(workflow)
        if media_type == "video" else detect_node_mapping(workflow, include_fast=True)
    )
    profile_id = workflow_profile_slug(name)
    directory = workflow_profile_directory(db, db_path, media_type)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile_id}.workflow.json"
    if path.exists() and replace:
        try:
            existing_workflow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_workflow = None
        if (
            isinstance(existing_workflow, dict)
            and workflow_is_video(existing_workflow) != (media_type == "video")
        ):
            existing_media = "Video" if workflow_is_video(existing_workflow) else "Image"
            raise AppError(
                f"Profile filename '{path.name}' is already used by an {existing_media} workflow. "
                "Choose a different name"
            )
    if path.exists() and not replace:
        raise AppError(f"Workflow profile '{name}' already exists. Confirm replacement to overwrite it")
    temporary = directory / f".{profile_id}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    registry = load_workflow_profile_registry(db, db_path, media_type)
    if not registry.get("production"):
        registry["production"] = profile_id
    if media_type == "image" and not registry.get("preview"):
        registry["preview"] = profile_id
    save_workflow_profile_registry(db, db_path, registry, media_type=media_type)
    return {
        "id": profile_id,
        "name": name.strip(),
        "file": path.name,
        "prompt_id": prompt_id,
        "seed_targets": len(mapping["inference_seed"]),
        "negative_conditioning": (
            mapping.get("negative_prompt") is not None
            if media_type == "image" else True
        ),
        "media_type": media_type,
    }


def select_workflow_profiles(
    db: dict[str, Any], db_path: Path, production: str, preview: str = "",
    source: str = "profiles", media_type: str = "image",
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    available = list_workflow_profiles(db, db_path, media_type)
    valid = {item["id"] for item in available["profiles"] if item["valid"]}
    if source not in {"profiles", "live"}:
        raise AppError("Workflow source must be profiles or live")
    if source == "profiles" and production not in valid:
        raise AppError("Production must select a valid workflow profile")
    if media_type == "image" and source == "profiles" and preview not in valid:
        raise AppError("Preview must select a valid workflow profile")
    registry = (
        load_workflow_profile_registry(db, db_path, media_type)
        if source == "live"
        else {"production": production, **({"preview": preview} if media_type == "image" else {})}
    )
    save_workflow_profile_registry(db, db_path, registry, source, media_type)
    return list_workflow_profiles(db, db_path, media_type)


def rename_workflow_profile(
    db: dict[str, Any], db_path: Path, profile_id: str, name: str,
    media_type: str = "image",
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    old_id = workflow_profile_slug(profile_id)
    new_id = workflow_profile_slug(name)
    directory = workflow_profile_directory(db, db_path, media_type)
    source = directory / f"{old_id}.workflow.json"
    target = directory / f"{new_id}.workflow.json"
    if not source.is_file():
        raise AppError(f"Workflow profile not found: {old_id}")
    if target != source and target.exists():
        raise AppError(f"Workflow profile filename already exists: {target.name}")
    source.replace(target)
    registry = load_workflow_profile_registry(db, db_path, media_type)
    modes = ("production", "preview") if media_type == "image" else ("production",)
    for mode in modes:
        if registry.get(mode) == old_id:
            registry[mode] = new_id
    save_workflow_profile_registry(db, db_path, registry, media_type=media_type)
    return list_workflow_profiles(db, db_path, media_type)


def delete_workflow_profile(
    db: dict[str, Any], db_path: Path, profile_id: str, media_type: str = "image"
) -> dict[str, Any]:
    media_type = validate_media_type(media_type)
    profile_id = workflow_profile_slug(profile_id)
    registry = load_workflow_profile_registry(db, db_path, media_type)
    protected = {registry.get("production")}
    if media_type == "image":
        protected.add(registry.get("preview"))
    if profile_id in protected:
        raise AppError(
            "Select another Production and Preview profile before deleting this one"
            if media_type == "image" else
            "Select another Video Production profile before deleting this one"
        )
    path = workflow_profile_directory(db, db_path, media_type) / f"{profile_id}.workflow.json"
    if not path.is_file():
        raise AppError(f"Workflow profile not found: {profile_id}")
    path.unlink()
    return list_workflow_profiles(db, db_path, media_type)


def patch_workflow(workflow: dict[str, Any], mapping: dict[str, Any], positive: str, negative: str, seed: int) -> None:
    for map_key, value in (("positive_prompt", positive), ("negative_prompt", negative)):
        target = mapping[map_key]
        if target is None:
            continue
        try:
            workflow[target["node"]]["inputs"][target["input"]] = value
        except KeyError as exc:
            raise AppError(f"Detected workflow target {map_key} is missing: {exc}") from exc
    for target in mapping["inference_seed"]:
        try:
            workflow[target["node"]]["inputs"][target["input"]] = seed
        except KeyError as exc:
            raise AppError(f"Workflow no longer matches inference seed mapping: missing {exc}") from exc


def lora_rule_matches(scene: dict[str, Any], when: dict[str, list[str]]) -> bool:
    stage = scene["stage"]
    visible_slots = set(stage.get("visible_slots", []))
    body_visibility = set(stage.get("body_visibility", []))
    visible_garment_tags = set().union(*(
        tags(item) for slot, item in scene["outfit"]["garments"].items()
        if slot in visible_slots
    ), set())
    scalar_sets = {
        "stage_levels": {stage["level"]},
        "visual_categories": {stage.get("visual_category", "")},
        "visible_slots": visible_slots,
        "body_visibility": body_visibility,
        "visible_garment_tags": visible_garment_tags,
    }
    for field, required_values in when.items():
        suffix = next(
            (candidate for candidate in ("_any", "_all", "_none") if field.endswith(candidate)),
            None,
        )
        base = field.removesuffix(suffix) if suffix else field
        available = scalar_sets[base]
        required = set(required_values)
        if suffix == "_all" and not required <= available:
            return False
        if suffix == "_none" and required & available:
            return False
        if suffix in {"_any", None} and not required & available:
            return False
    return True


def apply_workflow_lora_rules(
    workflow: dict[str, Any], mapping: dict[str, Any], db: dict[str, Any],
    scene: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply matching database rules in declaration order, ignoring runtime misses."""
    targets_by_name: dict[str, list[dict[str, Any]]] = {}
    for target in mapping.get("loras", []):
        targets_by_name.setdefault(target["lora_name"], []).append(target)
    applied: list[dict[str, Any]] = []
    for rule in db["settings"].get("workflow_lora_rules", []):
        name = rule["lora_name"]
        if not lora_rule_matches(scene, rule["when"]):
            continue
        targets = targets_by_name.get(name, [])
        if not targets:
            continue
        nodes = []
        for target in targets:
            try:
                node_inputs = workflow[target["node"]]["inputs"]
                updates = {
                    field: rule[field]
                    for field in ("strength_model", "strength_clip")
                    if field in rule and field in target["strength_inputs"]
                }
                if not updates:
                    continue
                node_inputs.update(updates)
                nodes.append(target["node"])
            except (KeyError, TypeError):
                continue
        if not nodes:
            continue
        applied.append({
            "rule_id": rule["id"],
            "lora_name": name,
            "nodes": nodes,
            **{
                field: rule[field] for field in ("strength_model", "strength_clip")
                if field in rule
            },
        })
    return applied


def preview_dimensions(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """Scale down without changing orientation, using latent-safe multiples of 64."""
    scale = min(1.0, max_edge / max(width, height))
    scaled_width = max(64, min(max_edge, int(width * scale // 64) * 64))
    scaled_height = max(64, min(max_edge, int(height * scale // 64) * 64))
    return scaled_width, scaled_height


def prepare_fast_workflow(
    workflow: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    fast_mapping = mapping.get("fast_mode")
    if not isinstance(fast_mapping, dict) or not fast_mapping.get("output_targets"):
        raise AppError("Fast workflow mapping was not detected")
    dimensions = fast_mapping.get("dimensions")
    if not isinstance(dimensions, dict):
        raise AppError("Fast workflow has no safely detected latent dimensions")
    config, _ = load_config()
    width, height = preview_dimensions(
        dimensions["width"], dimensions["height"],
        config["comfy"]["preview_max_edge"],
    )
    try:
        latent_inputs = workflow[dimensions["node"]]["inputs"]
        latent_inputs[dimensions["width_input"]] = width
        latent_inputs[dimensions["height_input"]] = height
    except KeyError as exc:
        raise AppError(
            f"Detected preview dimension target is no longer valid: {exc}"
        ) from exc
    output_nodes = []
    for target in fast_mapping["output_targets"]:
        try:
            source = target["source"]
            if (
                not isinstance(source, list) or len(source) != 2
                or source[0] not in workflow
            ):
                raise KeyError(f"invalid source {source}")
            workflow[target["node"]]["inputs"][target["input"]] = source
            output_nodes.append(target["node"])
        except (KeyError, TypeError) as exc:
            raise AppError(
                f"Detected fast workflow target is no longer valid: {exc}"
            ) from exc

    required: set[str] = set()
    pending = list(output_nodes)
    while pending:
        node_id = pending.pop()
        if node_id in required:
            continue
        node = workflow.get(node_id)
        if node is None:
            raise AppError(f"Fast workflow references missing node: {node_id}")
        required.add(node_id)
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) in workflow:
                pending.append(str(value[0]))
    return {node_id: node for node_id, node in workflow.items() if node_id in required}


def wait_for_outputs(session: Any, url: str, prompt_id: str) -> dict[str, Any]:
    config, _ = load_config()
    comfy = config["comfy"]
    deadline = time.monotonic() + float(comfy["generation_timeout_seconds"])
    interval = float(comfy["poll_interval_seconds"])
    timeout = float(comfy["http_timeout_seconds"])
    while time.monotonic() < deadline:
        response = session.get(f"{url}/history/{prompt_id}", timeout=timeout)
        response.raise_for_status()
        item = response.json().get(prompt_id)
        if item:
            status = item.get("status", {})
            if status.get("completed") and status.get("status_str") == "success":
                return item.get("outputs", {})
            if status.get("status_str") == "error" or any(message[0] == "execution_error" for message in status.get("messages", [])):
                raise AppError(f"ComfyUI execution failed for prompt_id {prompt_id}")
        time.sleep(interval)
    raise AppError(f"Timed out waiting for prompt_id {prompt_id}")


def valhalla_prompt_request(workflow: dict[str, Any]) -> dict[str, Any]:
    """Mark submissions so live mode never reuses Valhalla's transformed graph."""
    return {
        "prompt": workflow,
        "client_id": f"valhalla-{uuid.uuid4()}",
        "extra_data": {"valhalla_origin": True},
    }


def load_workflow_runtime(
    db: dict[str, Any], db_path: Path, fast: bool, profile_id: str | None = None,
    media_type: str = "image",
) -> tuple[dict[str, Any], dict[str, Any]]:
    media_type = validate_media_type(media_type)
    if media_type == "video" and fast:
        raise AppError("Video workflows do not have a Preview tier")
    mode = "preview" if fast else "production"
    registry = load_workflow_profile_registry(db, db_path, media_type)
    selected = profile_id or registry.get(mode)
    if not selected:
        raise AppError(f"No {mode} workflow profile is selected. Capture or select one in Studio files")
    selected = workflow_profile_slug(str(selected))
    workflow_path = workflow_profile_directory(db, db_path, media_type) / f"{selected}.workflow.json"
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AppError(f"Selected {mode} workflow profile is missing: {workflow_path.name}") from exc
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid workflow JSON in {workflow_path}: {exc}") from exc
    if not isinstance(workflow, dict) or not workflow:
        raise AppError(f"Workflow must be a non-empty JSON object: {workflow_path}")
    mapping = (
        detect_video_node_mapping(workflow)
        if media_type == "video" else detect_node_mapping(workflow, include_fast=fast)
    )
    return workflow, mapping


def snapshot_live_workflow(
    db: dict[str, Any], fast: bool, media_type: str = "image"
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Freeze the latest compatible external ComfyUI workflow for one queued task."""
    media_type = validate_media_type(media_type)
    if media_type == "video" and fast:
        raise AppError("Video workflows do not have a Preview tier")
    prompt_id, workflow = latest_comfy_workflow(db, media_type)
    mapping = (
        detect_video_node_mapping(workflow)
        if media_type == "video" else detect_node_mapping(workflow, include_fast=fast)
    )
    model = workflow_model_name(workflow)
    label = f"Live ComfyUI · {model} · {prompt_id[:10]}"
    return label, prompt_id, copy.deepcopy(workflow), mapping


def encode_output_image(content: bytes, storage: dict[str, Any]) -> tuple[bytes, str]:
    if Image is None or ImageOps is None:
        raise AppError("Output image conversion requires Pillow; restart with launcher.sh to install it")
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            exif = source.info.get("exif")
            image = ImageOps.exif_transpose(source) if storage["strip_exif"] else source.copy()
            output_format = storage["output_format"]
            save_options: dict[str, Any] = {}
            if not storage["strip_exif"] and exif:
                save_options["exif"] = exif
            if output_format in {"jpeg", "jpg"}:
                if image.mode not in {"RGB", "L"}:
                    if "A" in image.getbands():
                        background = Image.new("RGB", image.size, "#111318")
                        background.paste(image, mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
                save_options.update(quality=storage["jpeg_quality"], optimize=True)
                suffix, pillow_format = ".jpg", "JPEG"
            else:
                suffix, pillow_format = ".png", "PNG"
            buffer = BytesIO()
            image.save(buffer, format=pillow_format, **save_options)
            return buffer.getvalue(), suffix
    except (OSError, ValueError) as exc:
        raise AppError(f"Could not encode generated image: {exc}") from exc


def generate_one(
    db: dict[str, Any],
    db_path: Path,
    positive: str,
    negative: str,
    seed: int,
    mode: str,
    shot_index: int,
    photoshoot_index: int,
    run_id: str,
    render_tier: str,
    fast: bool,
    workflow_template: dict[str, Any],
    mapping: dict[str, Any],
    scene: dict[str, Any] | None = None,
    lora_report: list[dict[str, Any]] | None = None,
) -> tuple[str, list[Path]]:
    workflow = copy.deepcopy(workflow_template)
    patch_workflow(workflow, mapping, positive, negative, seed)
    if scene is not None:
        applied = apply_workflow_lora_rules(workflow, mapping, db, scene)
        if lora_report is not None:
            lora_report.extend(applied)
    if fast:
        workflow = prepare_fast_workflow(workflow, mapping)
    session, url, timeout = comfy_session(db)
    try:
        response = session.post(
            f"{url}/prompt", json=valhalla_prompt_request(workflow), timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise AppError(f"Could not queue ComfyUI workflow: {exc}") from exc
    if payload.get("node_errors"):
        raise AppError(f"ComfyUI rejected workflow: {json.dumps(payload['node_errors'], ensure_ascii=False)}")
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        raise AppError(f"ComfyUI response has no prompt_id: {payload}")
    outputs = wait_for_outputs(session, url, prompt_id)
    config, config_file = load_config()
    output_dir = resolve_path(config_file.parent, config["storage"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    image_number = 0
    for node_output in outputs.values():
        for image in node_output.get("images", []):
            image_number += 1
            group_index = photoshoot_index + 1 if mode == "photoshoot" else 1
            label = (
                f"{mode}_{group_index:03d}_{render_tier}_"
                f"shot_{shot_index + 1:03d}"
            )
            response = session.get(
                f"{url}/view",
                params={"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")},
                timeout=timeout,
            )
            response.raise_for_status()
            encoded, suffix = encode_output_image(response.content, config["storage"])
            destination = output_dir / f"{run_id}_{label}_{seed}_image_{image_number:02d}{suffix}"
            temporary = output_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(encoded)
            temporary.replace(destination)
            saved.append(destination)
    if not saved:
        raise AppError(f"ComfyUI completed prompt_id {prompt_id} but returned no images")
    return prompt_id, saved


def upload_comfy_image(
    session: Any, url: str, timeout: float, source_path: Path
) -> str:
    try:
        with source_path.open("rb") as handle:
            response = session.post(
                f"{url}/upload/image",
                files={"image": (source_path.name, handle, mimetypes.guess_type(source_path.name)[0] or "application/octet-stream")},
                data={"type": "input", "overwrite": "true"},
                timeout=timeout,
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise AppError(f"Could not upload source image to ComfyUI: {exc}") from exc
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise AppError(f"ComfyUI upload response has no image name: {payload}")
    return name


def patch_video_workflow(
    workflow: dict[str, Any], mapping: dict[str, Any], image_name: str,
    prompt: str, seed: int, duration: int,
) -> None:
    targets = list(mapping.get("image_targets", []))
    try:
        for target in targets:
            workflow[target["node"]]["inputs"][target["input"]] = image_name
        target = mapping["prompt"]
        workflow[target["node"]]["inputs"][target["input"]] = prompt
        for target in mapping["inference_seed"]:
            workflow[target["node"]]["inputs"][target["input"]] = seed
        target = mapping["duration"]
        workflow[target["node"]]["inputs"][target["input"]] = duration
    except (KeyError, TypeError) as exc:
        raise AppError(f"Video workflow mapping is no longer valid: {exc}") from exc


def generate_video_one(
    db: dict[str, Any], source_path: Path, source_item: dict[str, Any],
    prompt: str, seed: int, duration: int, workflow_template: dict[str, Any],
    mapping: dict[str, Any], run_id: str,
) -> tuple[str, list[Path]]:
    workflow = copy.deepcopy(workflow_template)
    session, url, timeout = comfy_session(db)
    image_name = upload_comfy_image(session, url, timeout, source_path)
    patch_video_workflow(workflow, mapping, image_name, prompt, seed, duration)
    try:
        response = session.post(
            f"{url}/prompt", json=valhalla_prompt_request(workflow), timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise AppError(f"Could not queue ComfyUI video workflow: {exc}") from exc
    if payload.get("node_errors"):
        raise AppError(f"ComfyUI rejected video workflow: {json.dumps(payload['node_errors'], ensure_ascii=False)}")
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        raise AppError(f"ComfyUI response has no prompt_id: {payload}")
    outputs = wait_for_outputs(session, url, prompt_id)
    config, config_file = load_config()
    output_dir = resolve_path(config_file.parent, config["storage"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    source_token = source_media_token(
        str(source_item.get("source", "output")),
        str(source_item.get("relative_path", source_path.name)),
        source_path.name,
    )
    saved: list[Path] = []
    number = 0
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        video_outputs = [
            video for output_key in ("videos", "gifs")
            for video in (node_output.get(output_key) if isinstance(node_output.get(output_key), list) else [])
            if isinstance(video, dict)
        ]
        image_outputs = node_output.get("images")
        candidates = video_outputs + [
            image for image in (image_outputs if isinstance(image_outputs, list) else [])
            if isinstance(image, dict)
            and Path(str(image.get("filename", ""))).suffix.lower() in VIDEO_SUFFIXES
        ]
        if mapping.get("output_nodes") and node_id not in mapping["output_nodes"]:
            continue
        for video in candidates:
            number += 1
            filename = str(video.get("filename", ""))
            suffix = Path(filename).suffix.lower() if filename else ".mp4"
            if suffix not in VIDEO_SUFFIXES:
                suffix = ".mp4"
            response = session.get(
                f"{url}/view",
                params={
                    "filename": filename,
                    "subfolder": video.get("subfolder", ""),
                    "type": video.get("type", "output"),
                },
                timeout=timeout,
            )
            response.raise_for_status()
            destination = output_dir / f"{run_id}_video_from_{source_token}_{seed}_video_{number:02d}{suffix}"
            temporary = output_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(response.content)
            temporary.replace(destination)
            saved.append(destination)
    if not saved:
        raise AppError(f"ComfyUI completed video prompt_id {prompt_id} but returned no video files")
    return prompt_id, saved


def generate_preview_image(
    db: dict[str, Any],
    positive: str,
    negative: str,
    seed: int,
    workflow_template: dict[str, Any],
    mapping: dict[str, Any],
    scene: dict[str, Any] | None = None,
    lora_report: list[dict[str, Any]] | None = None,
) -> tuple[str, bytes, str]:
    """Render one fast preview and keep its bytes out of the output directory."""
    workflow = copy.deepcopy(workflow_template)
    patch_workflow(workflow, mapping, positive, negative, seed)
    if scene is not None:
        applied = apply_workflow_lora_rules(workflow, mapping, db, scene)
        if lora_report is not None:
            lora_report.extend(applied)
    workflow = prepare_fast_workflow(workflow, mapping)
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            node["class_type"] = "PreviewImage"
            node.get("inputs", {}).pop("filename_prefix", None)
    session, url, timeout = comfy_session(db)
    try:
        response = session.post(
            f"{url}/prompt",
            json=valhalla_prompt_request(workflow),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise AppError(f"Could not queue ComfyUI preview workflow: {exc}") from exc
    if payload.get("node_errors"):
        raise AppError(
            f"ComfyUI rejected preview workflow: "
            f"{json.dumps(payload['node_errors'], ensure_ascii=False)}"
        )
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        raise AppError(f"ComfyUI response has no prompt_id: {payload}")
    outputs = wait_for_outputs(session, url, prompt_id)
    for node_output in outputs.values():
        for image in node_output.get("images", []):
            response = session.get(
                f"{url}/view",
                params={
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                },
                timeout=timeout,
            )
            response.raise_for_status()
            mime_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0]
                or mimetypes.guess_type(image.get("filename", "preview.png"))[0]
                or "image/png"
            )
            return prompt_id, response.content, mime_type
    raise AppError(f"ComfyUI completed preview {prompt_id} but returned no images")


def build_storyboard(
    args: argparse.Namespace,
    db: dict[str, Any],
    composer: Composer,
    rng: random.Random,
    nsfw_percent: float,
    plateau_percent: float,
) -> list[dict[str, Any]]:
    photoshoot_count = args.photoshoots if args.mode == "photoshoot" else 1
    seen_photoshoots: set[tuple[Any, ...]] = set()
    storyboard: list[dict[str, Any]] = []
    for photoshoot_index in range(photoshoot_count):
        avoid: dict[str, set[str]] = {}
        removed_slots: set[str] = set()
        xxx_plan = (
            full_xxx_recipe_plan(db, args.count, rng)
            if args.content_mode == "xxx" else []
        )
        progressive_xxx_plan: list[dict[str, Any]] = []
        progressive_xxx_index = 0
        bag_scope = f"photoshoot-{photoshoot_index}"
        fixed = None
        if args.mode == "photoshoot":
            attempts = composer.max_scene_attempts
            for _ in range(attempts):
                candidate = composer.fixed_context(args.content_mode)
                signature = photoshoot_signature(candidate)
                if signature not in seen_photoshoots:
                    fixed = candidate
                    seen_photoshoots.add(signature)
                    break
            if fixed is None:
                raise AppError(
                    f"Could not assemble distinct photoshoot {photoshoot_index + 1} "
                    f"after {attempts} attempts"
                )
        for shot_index in range(args.count):
            context = fixed if fixed is not None else composer.fixed_context(args.content_mode)
            assert context is not None
            template = context["outfit"]["template"]
            stage = (
                sfw_stage(template, shot_index, args.count, args.mode, rng)
                if args.content_mode == "sfw"
                else (
                    full_xxx_stage(template, xxx_plan[shot_index], shot_index)
                    if args.content_mode == "xxx"
                    else stage_for_index(
                        template, shot_index, args.count, args.mode, rng,
                        nsfw_percent, plateau_percent,
                    )
                )
            )
            if args.content_mode == "progressive":
                stage = maybe_dressed_panties_reveal(
                    db, stage, context["outfit"], rng
                )
            if (
                args.mode == "photoshoot"
                and args.content_mode == "progressive"
                and stage["level"] == "explicit"
            ):
                if not progressive_xxx_plan:
                    progressive_xxx_plan = full_xxx_recipe_plan(
                        db, args.count - shot_index, rng
                    )
                stage = full_xxx_stage(
                    template,
                    progressive_xxx_plan[progressive_xxx_index],
                    shot_index,
                )
                progressive_xxx_index += 1
            if args.mode == "photoshoot" and removed_slots:
                stage = copy.deepcopy(stage)
                stage["visible_slots"] = [
                    slot for slot in stage.get("visible_slots", [])
                    if slot not in removed_slots
                ]
            role_id = "role_peak" if stage.get("planned_intensity") == "peak" else {
                "covered": "role_establishing" if shot_index == 0 else "role_portrait",
                "lingerie": "role_development", "topless": "role_reveal", "nude": "role_nude_study",
                "explicit": "role_plateau",
            }.get(stage["level"], "portrait")
            overrides = {"editorial_role": role_id}
            if stage.get("planned_recipe_id"):
                overrides["explicit_recipe"] = stage["planned_recipe_id"]
                overrides["intensity"] = stage["planned_intensity"]
            try:
                scene = composer.resolve_scene(
                    context, stage, overrides, avoid, bag_scope
                )
            except AppError:
                # Diversity is a preference; compatibility always wins.
                scene = composer.resolve_scene(
                    context, stage, overrides, bag_scope=bag_scope
                )
            previous_scene = (
                storyboard[-1]["scene"]
                if storyboard
                and storyboard[-1]["photoshoot_index"] == photoshoot_index
                else None
            )
            tuple_keys = ("pose", "action", "shot_size", "camera_angle")
            if previous_scene:
                previous_tuple = tuple(previous_scene[key]["id"] for key in tuple_keys)
                for _ in range(composer.max_scene_attempts):
                    current_tuple = tuple(scene[key]["id"] for key in tuple_keys)
                    if current_tuple != previous_tuple:
                        break
                    scene = composer.resolve_scene(
                        context, stage, overrides, bag_scope=bag_scope
                    )
                else:
                    raise AppError(
                        "Could not avoid an adjacent pose/action/camera tuple repeat"
                    )
            previous = storyboard[-1] if storyboard and storyboard[-1]["photoshoot_index"] == photoshoot_index else None
            previous_slots = set(previous["stage"].get("visible_slots", [])) if previous else set(stage.get("visible_slots", []))
            current_slots = set(stage.get("visible_slots", []))
            if args.mode == "photoshoot" and current_slots & removed_slots:
                restored = sorted(current_slots & removed_slots)
                raise AppError(
                    f"Progressive garment state restored removed slots: {restored}"
                )
            removed = previous_slots - current_slots
            if args.mode == "photoshoot":
                removed_slots |= removed
            if removed:
                previous_scene = previous["scene"] if previous else None
                if previous_scene:
                    for slot in sorted(removed):
                        garment = context["outfit"]["garments"].get(slot)
                        states = (garment or {}).get("supported_states", [])
                        intermediate = next((
                            state for state in states
                            if state not in {"worn_closed", "removed"}
                        ), None)
                        if not intermediate:
                            continue
                        previous_scene.setdefault("garment_states", {})[slot] = intermediate
                        previous_scene["stage"].setdefault("garment_states", {})[slot] = intermediate
                        if intermediate == "unbuttoned_open" and "bra" in context["outfit"]["garments"]:
                            previous_scene["stage"]["visible_slots"] = list(dict.fromkeys([
                                *previous_scene["stage"].get("visible_slots", []), "bra",
                            ]))
                        if intermediate == "lowered_to_hips" and "panties" in context["outfit"]["garments"]:
                            previous_scene["stage"]["visible_slots"] = list(dict.fromkeys([
                                *previous_scene["stage"].get("visible_slots", []), "panties",
                            ]))
                names = [
                    context["outfit"]["garments"][slot]["prompt"]
                    for slot in context["outfit"]["template"]["slots"]
                    if slot in removed and slot in context["outfit"]["garments"]
                ]
                if names:
                    scene["garment_transition"] = {
                        "id": "transition_" + "_".join(sorted(removed)),
                        "prompt": "fully removed and no longer wearing " + " and ".join(names),
                        "slots": sorted(removed),
                    }
            scene["removed_garment_slots"] = sorted(removed_slots)
            for key in ("furniture", "pose", "action", "expression", "editorial_role", "shot_size", "camera_angle", "framing", "focus_target", "explicit_recipe", "intimate_arousal_modifier"):
                item = scene.get(key)
                if item:
                    avoid.setdefault(key, set()).add(item["id"])
            inference_seed = args.inference_seed
            if args.inference_strategy == "random":
                inference_seed = automatic_ui_seed()
            elif args.inference_strategy == "sequence":
                material = f"{args.inference_seed}:{photoshoot_index}:{shot_index}".encode()
                inference_seed = deterministic_ui_seed(material)
            storyboard.append({
                "number": len(storyboard) + 1,
                "photoshoot_index": photoshoot_index,
                "shot_index": shot_index,
                "context": context,
                "stage": stage,
                "scene": scene,
                "inference_seed": inference_seed,
            })
    return storyboard


def camera_grammar_stress_test(
    db: dict[str, Any], count: int = 10_000, seed: int = 20260721
) -> dict[str, Any]:
    """Resolve a deterministic scene sample and fail on the first camera conflict."""
    rng = random.Random(seed)
    composer = Composer(db, rng)
    checked = 0
    recipes: set[str] = set()
    tuples: set[tuple[str, str, str, str]] = set()
    selected_ids = {
        key: set() for key in (
            "poses", "actions", "expressions", "shot_sizes", "camera_angles",
            "framings", "focus_targets",
        )
    }
    context: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = []
    for index in range(count):
        if context is None or index % 100 == 0:
            context = composer.fixed_context()
            stages = effective_photoshoot_stages(context["outfit"]["template"])
        stage = stages[index % len(stages)]
        stage = maybe_dressed_panties_reveal(
            db, stage, context["outfit"], rng
        )
        scene = composer.resolve_scene(context, stage)
        validate_camera_grammar(scene)
        checked += 1
        if scene.get("explicit_recipe"):
            recipes.add(scene["explicit_recipe"]["id"])
        for key, section in (
            ("pose", "poses"), ("action", "actions"),
            ("expression", "expressions"), ("shot_size", "shot_sizes"),
            ("camera_angle", "camera_angles"), ("framing", "framings"),
            ("focus_target", "focus_targets"),
        ):
            selected_ids[section].add(scene[key]["id"])
        tuples.add(tuple(
            scene[key]["id"]
            for key in ("shot_size", "camera_angle", "framing", "focus_target")
        ))
    return {
        "checked": checked,
        "recipes": sorted(recipes),
        "camera_tuples": len(tuples),
        "selected_ids": {
            section: sorted(item_ids) for section, item_ids in selected_ids.items()
        },
    }


def _human_trait_reachability(
    db: dict[str, Any], use_defaults: bool,
) -> set[str]:
    """Enumerate exact dependency states while discarding tags no later rule reads."""
    parts = db["human_model_parts"]
    order = [category for category in HUMAN_SELECTION_ORDER if category in parts]
    order.extend(category for category in parts if category not in order)
    referenced_by_category = [
        set().union(*(
            set(item.get("requires_tags", []))
            | set(item.get("requires_any_tags", []))
            | set(item.get("excludes_tags", []))
            for item in parts[category]
        ))
        for category in order
    ]
    future_tags: list[set[str]] = [set() for _ in order]
    accumulated: set[str] = set()
    for index in range(len(order) - 1, -1, -1):
        accumulated |= referenced_by_category[index]
        future_tags[index] = set(accumulated)
    defaults = db["settings"].get("human_defaults", {}).get("pools", {})
    states: set[frozenset[str]] = {frozenset()}
    reachable: set[str] = set()
    for index, category in enumerate(order):
        candidates = [
            item for item in parts[category] if not item.get("disabled", False)
        ]
        if use_defaults and defaults.get(category):
            allowed = set(defaults[category])
            candidates = [item for item in candidates if item["id"] in allowed]
        next_states: set[frozenset[str]] = set()
        relevant_later = future_tags[index + 1] if index + 1 < len(order) else set()
        for state in states:
            available = set(state)
            for item in candidates:
                if not compatible_with_requirements(item, available):
                    continue
                reachable.add(item["id"])
                next_states.add(frozenset(
                    (available | tags(item)) & relevant_later
                ))
        states = next_states
        if not states:
            break
    return reachable


def catalog_reachability(db: dict[str, Any]) -> dict[str, Any]:
    """Compute deterministic manual and automatic catalog reachability."""
    enabled = {
        section: [item for item in db[section] if not item.get("disabled", False)]
        for section in (
            "colors", "patterns", "fabric_textures", "outfit_templates",
            "interiors", "furniture", "location_zones", "poses", "actions",
            "props", "expressions", "moods", "photography_styles",
            "shot_sizes", "camera_angles", "framings", "focus_targets",
            "editorial_roles", "explicit_recipes", "intimate_arousal_modifiers",
        )
    }
    reachable: dict[str, set[str]] = {section: set() for section in enabled}
    automatic: dict[str, set[str]] = {section: set() for section in enabled}
    candidate_pools: list[dict[str, Any]] = []

    garment_enabled = {
        section: [item for item in values if not item.get("disabled", False)]
        for section, values in db["garments"].items()
    }
    garment_reachable: set[str] = set()
    garment_automatic: set[str] = set()
    template_stage_states: list[tuple[dict[str, Any], set[str]]] = []
    wardrobe_categories = set(
        db["settings"]["scene_defaults"]["wardrobe_categories"]
    )
    for template in enabled["outfit_templates"]:
        template_has_required_candidates = True
        visible_tag_unions: dict[str, set[str]] = {}
        visible_id_unions: dict[str, set[str]] = {}
        for slot, rule in template["slots"].items():
            candidates = [
                item for item in garment_enabled[rule["catalog"]]
                if garment_matches_template_slot(
                    db, template, slot, item, "progressive"
                )
            ]
            preferred = prefer_catalog_category(
                candidates, catalog_category(template)
            )
            candidate_pools.append({
                "kind": "garment_slot",
                "id": f"{template['id']}.{slot}",
                "size": len(preferred),
                "manual_size": len(candidates),
            })
            garment_reachable.update(item["id"] for item in candidates)
            if catalog_category(template) in wardrobe_categories:
                garment_automatic.update(item["id"] for item in preferred)
            if rule.get("required", False) and not candidates:
                template_has_required_candidates = False
            visible_tag_unions[slot] = set().union(
                *(tags(item) for item in candidates), set()
            )
            visible_id_unions[slot] = {item["id"] for item in candidates}
        if template_has_required_candidates:
            reachable["outfit_templates"].add(template["id"])
            if catalog_category(template) in wardrobe_categories:
                automatic["outfit_templates"].add(template["id"])
        for stage in effective_photoshoot_stages(template):
            garment_tags = set().union(*(
                visible_tag_unions.get(slot, set())
                for slot in stage.get("visible_slots", [])
            ), set())
            template_stage_states.append((stage, garment_tags))
            if stage["level"] == "explicit" and "panties" in template["slots"]:
                panties_aside = copy.deepcopy(stage)
                panties_aside["plateau_kind"] = "panties_aside"
                panties_aside["visible_slots"] = [
                    slot for slot in (
                        "panties", "legwear", "footwear", "accessories"
                    ) if slot in template["slots"]
                ]
                panties_aside["body_visibility"] = [
                    "breasts", "nipples", "pubic_area", "genitals"
                ]
                panties_tags = set().union(*(
                    visible_tag_unions.get(slot, set())
                    for slot in panties_aside["visible_slots"]
                ), set())
                template_stage_states.append((panties_aside, panties_tags))
        reveal_rule = db["settings"]["dressed_panties_reveal"]
        reveal_ids = set(reveal_rule["compatible_outer_ids"])
        reveal_stage = next((
            stage for stage in effective_photoshoot_stages(template)
            if stage["level"] == "covered"
            and "panties" in template["slots"]
            and any(
                slot in stage.get("visible_slots", [])
                and bool(visible_id_unions.get(slot, set()) & reveal_ids)
                for slot in reveal_rule["outer_slots"]
            )
        ), None)
        if reveal_stage:
            dressed_reveal = copy.deepcopy(reveal_stage)
            dressed_reveal["visual_category"] = "dressed_panties_reveal"
            dressed_reveal["visible_slots"] = list(dict.fromkeys([
                *dressed_reveal.get("visible_slots", []), "panties",
            ]))
            dressed_tags = set().union(*(
                visible_tag_unions.get(slot, set())
                for slot in dressed_reveal["visible_slots"]
            ), set())
            template_stage_states.append((dressed_reveal, dressed_tags))

    reachable["colors"].update(
        color["id"] for color in enabled["colors"]
        if any(
            not garment.get("allowed_colors")
            or color["id"] in garment.get("allowed_colors", [])
            for garments in garment_enabled.values() for garment in garments
            if garment["id"] in garment_reachable
        )
    )
    automatic["colors"] = set(reachable["colors"])
    for section in ("patterns", "fabric_textures"):
        for item in enabled[section]:
            if set(item["allowed_garment_ids"]) & garment_reachable:
                reachable[section].add(item["id"])
            if set(item["allowed_garment_ids"]) & garment_automatic:
                automatic[section].add(item["id"])

    environment_categories = set(
        db["settings"]["scene_defaults"]["environment_categories"]
    )
    environment_states: dict[
        tuple[frozenset[str], str], tuple[set[str], dict[str, Any]]
    ] = {}
    for interior in enabled["interiors"]:
        furniture_candidates = [
            item for item in enabled["furniture"]
            if compatible_with_requirements(item, tags(interior))
            and category_allows(interior, item)
        ]
        candidate_pools.append({
            "kind": "furniture",
            "id": interior["id"],
            "size": len(prefer_catalog_category(
                furniture_candidates, catalog_category(interior)
            )),
            "manual_size": len(furniture_candidates),
        })
        if furniture_candidates:
            reachable["interiors"].add(interior["id"])
            if catalog_category(interior) in environment_categories:
                automatic["interiors"].add(interior["id"])
        preferred = prefer_catalog_category(
            furniture_candidates, catalog_category(interior)
        )
        for furniture in furniture_candidates:
            zones = matching_location_zones(db, interior, furniture)
            if not zones:
                continue
            reachable["furniture"].add(furniture["id"])
            for zone in zones:
                reachable["location_zones"].add(zone["id"])
                state_tags = (
                    tags(interior) | tags(furniture)
                    | set(zone.get("capabilities", []))
                )
                environment_states[(frozenset(state_tags), zone["id"])] = (
                    state_tags, zone
                )
        if catalog_category(interior) in environment_categories:
            for furniture in preferred:
                zones = matching_location_zones(db, interior, furniture)
                if not zones:
                    continue
                automatic["furniture"].add(furniture["id"])
                automatic["location_zones"].update(
                    zone["id"] for zone in zones
                )

    reachable["moods"].update(item["id"] for item in enabled["moods"])
    reachable["photography_styles"].update(
        item["id"] for item in enabled["photography_styles"]
    )
    scene_pools = db["settings"]["scene_defaults"].get("pools", {})
    for section, pool_key in (
        ("moods", "moods"), ("photography_styles", "photography_styles"),
    ):
        configured = set(scene_pools.get(pool_key, []))
        automatic[section].update(
            item["id"] for item in enabled[section]
            if not configured or item["id"] in configured
        )
    reachable["editorial_roles"].update(
        item["id"] for item in enabled["editorial_roles"]
    )
    automatic["editorial_roles"].update(reachable["editorial_roles"])
    reachable["explicit_recipes"].update(
        item["id"] for item in enabled["explicit_recipes"]
    )
    automatic["explicit_recipes"].update(reachable["explicit_recipes"])

    direction_contexts: set[
        tuple[frozenset[str], str, str, str | None, str | None, str]
    ] = set()
    direction_sections = (
        "poses", "actions", "props", "shot_sizes", "camera_angles",
        "framings", "focus_targets",
    )
    direction_relevant_tags = set().union(*(
        set(item.get("requires_tags", []))
        | set(item.get("requires_any_tags", []))
        | set(item.get("excludes_tags", []))
        for section in direction_sections for item in enabled[section]
    ))
    camera_relevant_tags = set().union(*(
        set(item.get("requires_tags", []))
        | set(item.get("requires_any_tags", []))
        | set(item.get("excludes_tags", []))
        for section in ("shot_sizes", "camera_angles", "framings", "focus_targets")
        for item in enabled[section]
    ))
    recipes_by_id = {item["id"]: item for item in enabled["explicit_recipes"]}
    for stage, garment_tags in template_stage_states:
        recipes: list[dict[str, Any] | None] = [None]
        if stage["level"] == "explicit":
            recipes = [
                recipe for recipe in enabled["explicit_recipes"]
                if not stage.get("plateau_kind")
                or recipe.get("plateau_kind") == stage["plateau_kind"]
            ]
            if not recipes:
                recipes = [None]
        for environment_tags, zone in environment_states.values():
            base = (
                set(stage.get("body_visibility", []))
                | {stage["level"]}
                | set(stage.get("visible_slots", []))
                | garment_tags | environment_tags
            )
            if stage.get("visual_category"):
                base.add(stage["visual_category"])
            for recipe in recipes:
                available = base | (tags(recipe) if recipe else set())
                intensity = recipe.get("intensity", "explicit") if recipe else {
                    "covered": "fashion", "lingerie": "sensual",
                    "topless": "erotic", "nude": "nude",
                    "explicit": "explicit",
                }[stage["level"]]
                plateau = stage.get("plateau_kind") or (
                    recipe.get("plateau_kind") if recipe else None
                )
                direction_contexts.add((
                    frozenset(available & direction_relevant_tags),
                    stage["level"], intensity,
                    plateau, recipe["id"] if recipe else None, zone["id"],
                ))

    camera_contexts: set[
        tuple[frozenset[str], str, str, str | None, str | None, str, str]
    ] = set()
    zones_by_id = {item["id"]: item for item in enabled["location_zones"]}
    for frozen_tags, level, intensity, plateau, recipe_id, zone_id in direction_contexts:
        available = set(frozen_tags)
        recipe = recipes_by_id.get(recipe_id or "")
        pose_candidates = []
        for pose in enabled["poses"]:
            if (
                level not in pose.get("allowed_levels", [level])
                or not item_allows_intensity(pose, intensity)
                or not compatible_with_requirements(pose, available)
            ):
                continue
            if level == "explicit" and "explicit_pose" not in tags(pose):
                continue
            if level in {"topless", "nude"} and not (
                tags(pose) & {"erotic_pose", "topless_pose", "nude_pose", "open_legs"}
            ):
                continue
            if plateau == "provocative_rear" and "provocative_rear" not in tags(pose):
                continue
            if plateau == "intimate_closeup" and "intimate_closeup" not in tags(pose):
                continue
            if plateau == "masturbation" and "masturbation_pose" not in tags(pose):
                continue
            if plateau == "panties_aside" and (
                "open_legs" not in tags(pose) or "provocative_rear" in tags(pose)
            ):
                continue
            if recipe and recipe.get("pose_tags") and not set(
                recipe["pose_tags"]
            ).issubset(tags(pose)):
                continue
            if not recipe_focus_compatible(pose, recipe, "pose"):
                continue
            try:
                validate_pose_zone(pose, zones_by_id[zone_id])
            except AppError:
                continue
            pose_candidates.append(pose)
            reachable["poses"].add(pose["id"])
        for pose in pose_candidates:
            action_tags = available | tags(pose)
            for action in enabled["actions"]:
                if (
                    level not in action.get("allowed_levels", [level])
                    or not item_allows_intensity(action, intensity)
                    or not compatible_with_requirements(action, action_tags)
                    or hands_required(pose) + hands_required(action) > 2
                ):
                    continue
                if level == "explicit" and "explicit_action" not in tags(action):
                    continue
                if level in {"topless", "nude"} and not (
                    tags(action) & {"erotic_action", "undressing_action"}
                ):
                    continue
                required_plateau_tag = {
                    "provocative_rear": "provocative_action",
                    "intimate_closeup": "closeup_action",
                    "masturbation": "masturbation_action",
                    "panties_aside": "panties_aside_action",
                }.get(plateau or "")
                if required_plateau_tag and required_plateau_tag not in tags(action):
                    continue
                if recipe and recipe.get("action_tags") and not set(
                    recipe["action_tags"]
                ).issubset(tags(action)):
                    continue
                if not recipe_focus_compatible(action, recipe, "action"):
                    continue
                reachable["actions"].add(action["id"])
                required_expression = set(action.get("requires_expression_tags", []))
                for expression in enabled["expressions"]:
                    if not item_allows_intensity(expression, intensity):
                        continue
                    if required_expression and not required_expression.issubset(tags(expression)):
                        continue
                    if not required_expression and level in {"covered", "lingerie"} and expression["id"] not in {
                        "expression_confident", "expression_soft_smile",
                        "expression_dreamy", "expression_playful",
                        "expression_serene", "expression_shy_sultry",
                    }:
                        continue
                    if level in {"topless", "nude"} and tags(expression) & {
                        "pleasure_expression", "intense_pleasure_expression",
                    }:
                        continue
                    reachable["expressions"].add(expression["id"])
                required_prop = set(action.get("requires_prop_tags", []))
                for prop in enabled["props"]:
                    if (
                        compatible_with_requirements(prop, available | tags(action))
                        and (not required_prop or required_prop.issubset(tags(prop)))
                        and hands_required(pose) + hands_required(action) + hands_required(prop) <= 2
                    ):
                        reachable["props"].add(prop["id"])
                camera_contexts.add((
                    frozenset(
                        (available | tags(pose) | tags(action))
                        & camera_relevant_tags
                    ),
                    level, intensity, plateau, recipe_id, pose["id"], action["id"],
                ))

    index = {
        item["id"]: item for item in iter_content_items(db)
        if not item.get("disabled", False)
    }
    all_role_tags = set().union(
        *(tags(item) for item in enabled["editorial_roles"]), set()
    )
    for frozen_tags, level, intensity, plateau, recipe_id, pose_id, action_id in camera_contexts:
        recipe = recipes_by_id.get(recipe_id or "")
        camera_tags = set(frozen_tags) | all_role_tags
        stage = {"level": level, "plateau_kind": plateau}
        scene_base = {
            "stage": stage, "explicit_recipe": recipe,
            "pose": index[pose_id], "action": index[action_id],
        }

        def eligible(section: str, available: set[str]) -> list[dict[str, Any]]:
            return [
                item for item in enabled[section]
                if level in item.get("allowed_levels", [level])
                and item_allows_intensity(item, intensity)
                and compatible_with_requirements(item, available)
            ]

        shot_sizes = eligible("shot_sizes", camera_tags)
        recipe_shot_sizes = set(recipe_reference_ids(recipe, "shot_size"))
        if recipe_shot_sizes:
            shot_sizes = [x for x in shot_sizes if x["id"] in recipe_shot_sizes]
        for shot_size in shot_sizes:
            angle_tags = camera_tags | tags(shot_size)
            angles = eligible("camera_angles", angle_tags)
            recipe_angles = set(recipe_reference_ids(recipe, "camera_angle"))
            if recipe_angles:
                angles = [x for x in angles if x["id"] in recipe_angles]
            for angle in angles:
                framing_tags = angle_tags | tags(angle)
                for framing in eligible("framings", framing_tags):
                    focus_tags = framing_tags | tags(framing)
                    focuses = eligible("focus_targets", focus_tags)
                    recipe_focuses = set(recipe_reference_ids(recipe, "focus_target"))
                    if recipe_focuses:
                        focuses = [x for x in focuses if x["id"] in recipe_focuses]
                    for focus in focuses:
                        scene = {
                            **scene_base,
                            "shot_size": shot_size, "camera_angle": angle,
                            "framing": framing, "focus_target": focus,
                        }
                        try:
                            validate_camera_grammar(scene)
                        except AppError:
                            continue
                        reachable["shot_sizes"].add(shot_size["id"])
                        reachable["camera_angles"].add(angle["id"])
                        reachable["framings"].add(framing["id"])
                        reachable["focus_targets"].add(focus["id"])

    if any(
        recipe.get("focus_target") == "focus_intimate"
        for recipe in enabled["explicit_recipes"]
    ):
        reachable["intimate_arousal_modifiers"].update(
            item["id"] for item in enabled["intimate_arousal_modifiers"]
        )
    for section in (
        "poses", "actions", "props", "expressions", "shot_sizes",
        "camera_angles", "framings", "focus_targets",
        "intimate_arousal_modifiers",
    ):
        automatic[section] = set(reachable[section])

    human_manual = _human_trait_reachability(db, False)
    human_automatic = _human_trait_reachability(db, True)
    human_enabled = {
        item["id"]
        for values in db["human_model_parts"].values()
        for item in values if not item.get("disabled", False)
    }
    section_enabled = {
        section: {item["id"] for item in items}
        for section, items in enabled.items()
    }
    section_enabled["garments"] = {
        item["id"] for items in garment_enabled.values() for item in items
    }
    reachable["garments"] = garment_reachable
    automatic["garments"] = garment_automatic
    section_enabled["human_traits"] = human_enabled
    reachable["human_traits"] = human_manual
    automatic["human_traits"] = human_automatic
    unreachable = {
        section: sorted(item_ids - reachable.get(section, set()))
        for section, item_ids in section_enabled.items()
        if item_ids - reachable.get(section, set())
    }
    return {
        "enabled": section_enabled,
        "reachable": reachable,
        "automatic": automatic,
        "unreachable": unreachable,
        "candidate_pools": candidate_pools,
    }


def validate_production_catalog(db: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic, GPU-free production reachability and stress checks."""
    templates = [
        item for item in db["outfit_templates"] if not item.get("disabled", False)
    ]
    interiors = [item for item in db["interiors"] if not item.get("disabled", False)]
    garments = [
        (section, item)
        for section, values in db["garments"].items()
        for item in values
        if not item.get("disabled", False)
    ]

    manual_garments: set[str] = set()
    automatic_garments: set[str] = set()
    for template in templates:
        for slot, rule in template["slots"].items():
            candidates = [
                garment for garment in db["garments"][rule["catalog"]]
                if garment_matches_template_slot(
                    db, template, slot, garment, "progressive"
                )
            ]
            manual_garments.update(item["id"] for item in candidates)
            preferred = prefer_catalog_category(
                candidates, catalog_category(template)
            )
            automatic_garments.update(item["id"] for item in preferred)
    unreachable = [
        f"{section}.{item['id']}"
        for section, item in garments if item["id"] not in manual_garments
    ]
    category_starved = [
        f"{section}.{item['id']}"
        for section, item in garments if item["id"] not in automatic_garments
    ]
    if unreachable:
        raise AppError(
            "Enabled garments unreachable from every outfit recipe: "
            + ", ".join(unreachable)
        )
    if category_starved:
        raise AppError(
            "Enabled garments removed by automatic catalog-category preference: "
            + ", ".join(category_starved)
        )

    reachability = catalog_reachability(db)
    if reachability["unreachable"]:
        details = "; ".join(
            f"{section}: {', '.join(item_ids)}"
            for section, item_ids in sorted(reachability["unreachable"].items())
        )
        raise AppError(f"Enabled catalog records have no compatible route: {details}")

    outfit_checks = 0
    sfw_outfit_checks = 0
    stage_checks = 0
    for template_index, template in enumerate(templates):
        for interior_index, interior in enumerate(interiors):
            composer = Composer(
                db, random.Random(700_000 + template_index * 1_000 + interior_index)
            )
            outfit = composer.choose_outfit(template, interior, "progressive")
            composer.validate_outfit_stage_coverage(outfit)
            outfit_checks += 1
            stage_checks += len(effective_photoshoot_stages(template))
            if template_supports_sfw(db, template):
                safe = composer.choose_outfit(template, interior, "sfw")
                validate_sfw_outfit(safe)
                sfw_outfit_checks += 1

    storyboard_checks = 0
    compiled_scenes = 0
    for mode in ("photoshoot", "random"):
        for content_mode in ("sfw", "progressive", "xxx"):
            for sample in range(4):
                seed = 800_000 + sample + (10_000 if mode == "random" else 0)
                seed += {"sfw": 0, "progressive": 1_000, "xxx": 2_000}[content_mode]
                run = parse_run_config({
                    "mode": mode,
                    "content_mode": content_mode,
                    "count": 12,
                    "photoshoots": 1,
                    "prompt_seed": seed,
                    "inference_seed": seed + 1,
                    "nsfw_percent": 50,
                    "plateau_percent": 20,
                }, db)
                rng = random.Random(run.prompt_seed)
                board = build_storyboard(
                    run, db, Composer(db, rng), rng,
                    run.nsfw_percent, run.plateau_percent,
                )
                removed_by_photoshoot: dict[int, set[str]] = {}
                for shot in board:
                    validate_camera_grammar(shot["scene"])
                    compile_scene(db, shot["scene"])
                    removed = removed_by_photoshoot.setdefault(
                        shot["photoshoot_index"], set()
                    )
                    visible = set(shot["stage"].get("visible_slots", []))
                    restored = visible & removed
                    if restored:
                        raise AppError(
                            "Progressive garment validation restored removed slots: "
                            f"{sorted(restored)}"
                        )
                    removed.update(shot["scene"].get("removed_garment_slots", []))
                    compiled_scenes += 1
                storyboard_checks += 1

    camera = camera_grammar_stress_test(db)
    enabled_recipes = {
        item["id"] for item in db["explicit_recipes"]
        if not item.get("disabled", False)
    }
    missing_recipes = sorted(enabled_recipes - set(camera["recipes"]))
    if missing_recipes:
        raise AppError(
            "Enabled explicit recipes missing from the camera stress sample: "
            + ", ".join(missing_recipes)
        )
    warnings: list[str] = []
    for section, observed in camera["selected_ids"].items():
        enabled = {
            item["id"] for item in db[section] if not item.get("disabled", False)
        }
        missing = sorted(enabled - set(observed))
        if missing:
            warnings.append(
                f"{section} not observed in deterministic camera sample: "
                + ", ".join(missing)
            )

    return {
        "templates": len(templates),
        "interiors": len(interiors),
        "garments": len(garments),
        "outfit_checks": outfit_checks,
        "sfw_outfit_checks": sfw_outfit_checks,
        "stage_checks": stage_checks,
        "storyboard_checks": storyboard_checks,
        "compiled_scenes": compiled_scenes,
        "camera_checks": camera["checked"],
        "camera_tuples": camera["camera_tuples"],
        "explicit_recipes": len(camera["recipes"]),
        "reachable_records": sum(
            len(item_ids) for item_ids in reachability["reachable"].values()
        ),
        "warnings": warnings,
    }


def print_validation_report(report: dict[str, Any]) -> None:
    print("Valhalla production validation passed")
    print(
        f"  wardrobe: {report['garments']} garments through "
        f"{report['templates']} templates"
    )
    print(
        f"  outfit matrix: {report['outfit_checks']} progressive and "
        f"{report['sfw_outfit_checks']} SFW combinations across "
        f"{report['interiors']} interiors"
    )
    print(
        f"  stages/storyboards: {report['stage_checks']} stage checks and "
        f"{report['compiled_scenes']} compiled scenes in "
        f"{report['storyboard_checks']} storyboards"
    )
    print(
        f"  camera grammar: {report['camera_checks']} scenes, "
        f"{report['camera_tuples']} tuples, "
        f"{report['explicit_recipes']} explicit recipes"
    )
    print(
        f"  exact reachability: {report['reachable_records']} enabled records "
        "have a compatible route"
    )
    for warning in report["warnings"]:
        print(f"  warning: {warning}")


def catalog_statistics(
    db: dict[str, Any], reachability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analysis = reachability or catalog_reachability(db)
    section_counts = {
        section: len(item_ids)
        for section, item_ids in analysis["enabled"].items()
    }
    tag_counts: dict[str, int] = {}
    for item in iter_content_items(db):
        if item.get("disabled", False):
            continue
        for tag in tags(item):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    pool_summaries: dict[str, dict[str, Any]] = {}
    for kind in ("garment_slot", "furniture"):
        rows = [row for row in analysis["candidate_pools"] if row["kind"] == kind]
        sizes = sorted(row["size"] for row in rows)
        middle = len(sizes) // 2
        median = (
            sizes[middle]
            if len(sizes) % 2
            else (sizes[middle - 1] + sizes[middle]) / 2
        )
        narrow = sorted(
            (row for row in rows if row["size"] <= 2),
            key=lambda row: (row["size"], row["id"]),
        )
        pool_summaries[kind] = {
            "count": len(rows),
            "minimum": min(sizes),
            "median": median,
            "maximum": max(sizes),
            "narrow": narrow,
        }
    return {
        "total_records": sum(section_counts.values()),
        "sections": section_counts,
        "manual_reachable": {
            section: len(analysis["reachable"].get(section, set()))
            for section in section_counts
        },
        "automatic_reachable": {
            section: len(analysis["automatic"].get(section, set()))
            for section in section_counts
        },
        "unreachable": analysis["unreachable"],
        "pool_summaries": pool_summaries,
        "unique_tags": len(tag_counts),
        "top_tags": sorted(
            tag_counts.items(), key=lambda item: (-item[1], item[0])
        )[:15],
    }


def print_catalog_statistics(report: dict[str, Any]) -> None:
    print(f"Valhalla catalog statistics: {report['total_records']} enabled records")
    print(
        f"  tags: {report['unique_tags']} unique; most common "
        + ", ".join(f"{tag}={count}" for tag, count in report["top_tags"])
    )
    for section in sorted(report["sections"]):
        enabled_count = report["sections"][section]
        manual = report["manual_reachable"][section]
        automatic_count = report["automatic_reachable"][section]
        print(
            f"  {section}: {enabled_count} enabled, {manual} reachable, "
            f"{automatic_count} in automatic pools"
        )
    if report["unreachable"]:
        for section, item_ids in sorted(report["unreachable"].items()):
            print(f"  unreachable {section}: {', '.join(item_ids)}")
    else:
        print("  unreachable: none")
    for kind, summary in report["pool_summaries"].items():
        print(
            f"  {kind} pools: {summary['count']} pools, "
            f"min/median/max {summary['minimum']}/{summary['median']:g}/"
            f"{summary['maximum']}, {len(summary['narrow'])} with at most 2 candidates"
        )
        for row in summary["narrow"][:20]:
            print(
                f"    narrow {row['id']}: {row['size']} automatic, "
                f"{row['manual_size']} manual"
            )
        if len(summary["narrow"]) > 20:
            print(f"    ... {len(summary['narrow']) - 20} more narrow pools")



# ---------------------------------------------------------------------------
# Web application
# ---------------------------------------------------------------------------

import mimetypes
import threading
import webbrowser
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None  # type: ignore[assignment]

CLIENT_ROOT = Path(__file__).resolve().with_name("client")
THUMBNAIL_CACHE: OrderedDict[tuple[str, str, int, int], bytes] = OrderedDict()
THUMBNAIL_CACHE_BYTES = 0
THUMBNAIL_CACHE_LOCK = threading.Lock()
THUMBNAIL_IN_FLIGHT: dict[tuple[str, str, int, int], Future[bytes]] = {}
PROMPT_DEBUG_LOG_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append_prompt_debug_record(record: dict[str, Any]) -> None:
    """Append one complete result/prompt mapping when opt-in logging is enabled."""
    config, config_file = load_config()
    settings = config["storage"].get("prompt_debug_log", {})
    if not settings.get("enabled", False):
        return
    path = resolve_path(config_file.parent, settings["path"])
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with PROMPT_DEBUG_LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except OSError as exc:
            raise AppError(f"Could not append prompt debug log {path}: {exc}") from exc


def _safe_int(
    value: Any, name: str, minimum: int = 1, maximum: int | None = 500
) -> int:
    if isinstance(value, bool):
        raise AppError(f"{name} must be a whole number")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{name} must be a whole number") from exc
    if parsed < minimum:
        if maximum is None:
            raise AppError(f"{name} must be at least {minimum}")
        raise AppError(f"{name} must be between {minimum} and {maximum}")
    if maximum is not None and parsed > maximum:
        raise AppError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _optional_seed(value: Any, name: str, maximum: int) -> int | None:
    if value in (None, ""):
        return None
    return _safe_int(value, name, 0, maximum)


def _safe_percent(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{name} must be a number") from exc
    if not 0 <= parsed <= 100:
        raise AppError(f"{name} must be between 0 and 100")
    return parsed


def parse_run_config(payload: dict[str, Any], db: dict[str, Any]) -> SimpleNamespace:
    obsolete = {"xxx_only", "sfw_only"} & set(payload)
    if obsolete:
        raise AppError(
            f"Obsolete content configuration: {', '.join(sorted(obsolete))}; use content_mode"
        )
    mode = payload.get("mode", "photoshoot")
    if mode not in {"photoshoot", "random"}:
        raise AppError("mode must be photoshoot or random")
    count = _safe_int(payload.get("count", 12), "Images", 1, None)
    photoshoots = _safe_int(payload.get("photoshoots", 1), "Photoshoots", 1, None)
    if mode == "random":
        photoshoots = 1
    prompt_seed = _optional_seed(payload.get("prompt_seed"), "Prompt seed", 2**63 - 1)
    inference_seed = _optional_seed(payload.get("inference_seed"), "Inference seed", 2**64 - 1)
    inference_strategy = str(payload.get("inference_strategy", "sequence"))
    if inference_strategy not in {"random", "fixed", "sequence"}:
        raise AppError("Inference seed strategy must be random, fixed, or sequence")
    if inference_strategy in {"fixed", "sequence"} and inference_seed is None:
        inference_seed = automatic_ui_seed()
    use_curated_defaults = payload.get("use_curated_defaults", True)
    if not isinstance(use_curated_defaults, bool):
        raise AppError("use_curated_defaults must be a boolean")
    content_mode = payload.get("content_mode", "progressive")
    if content_mode not in {"sfw", "progressive", "xxx"}:
        raise AppError("content_mode must be sfw, progressive, or xxx")
    progression = db["settings"].get("photoshoot_progression", {})
    nsfw = _safe_percent(
        payload.get("nsfw_percent", progression.get("nsfw_final_percent", 50)),
        "NSFW ending",
    )
    plateau = _safe_percent(
        payload.get("plateau_percent", progression.get("explicit_plateau_percent", 30)),
        "Explicit plateau",
    )
    if plateau > nsfw:
        raise AppError("The explicit plateau percentage cannot exceed the NSFW ending")
    return SimpleNamespace(
        mode=mode,
        count=count,
        photoshoots=photoshoots,
        prompt_seed=prompt_seed,
        inference_seed=inference_seed,
        inference_strategy=inference_strategy,
        content_mode=content_mode,
        nsfw_percent=None if content_mode != "progressive" or mode == "random" else nsfw,
        plateau_percent=None if content_mode != "progressive" or mode == "random" else plateau,
        use_curated_defaults=use_curated_defaults,
        fast=bool(payload.get("fast", False)),
    )


def _args_dict(args: SimpleNamespace) -> dict[str, Any]:
    return {key: value for key, value in vars(args).items() if key != "review_storyboard"}


def _outfit_summary(outfit: dict[str, Any]) -> list[str]:
    result = []
    for slot, garment in outfit["garments"].items():
        color = outfit.get("colors", {}).get(slot, {}).get("prompt")
        result.append(f"{color} {garment['prompt']}" if color else garment["prompt"])
    return result


def _surface_summary(scene: dict[str, Any]) -> str:
    custom = scene.get("custom_values", {})
    parts = [custom.get("shot.furniture") or scene["furniture"]["prompt"]]
    for kind in ("color", "texture"):
        item = scene.get(f"surface_{kind}")
        prompt = custom.get(f"shot.surface_{kind}") or (
            item.get("prompt") if item else None
        )
        if prompt:
            parts.append(prompt)
    return " · ".join(parts)


def serialize_shot(db: dict[str, Any], shot: dict[str, Any]) -> dict[str, Any]:
    scene = shot["scene"]
    context = shot["context"]
    positive, negative, selected_ids = compile_scene(db, scene)
    template = context["outfit"]["template"]
    return {
        "number": shot["number"],
        "photoshoot_index": shot["photoshoot_index"],
        "shot_index": shot["shot_index"],
        "stage": {
            "id": shot["stage"]["id"],
            "level": shot["stage"]["level"],
            "plateau_kind": shot["stage"].get("plateau_kind"),
            "manual": bool(shot.get("stage_manual", False)),
        },
        "inference_seed": shot["inference_seed"],
        "seed_manual": bool(shot.get("seed_manual", False)),
        "manual_fields": sorted(set(shot.get("manual_fields", []))),
        "subject": model_description(context["human"], scene.get("custom_values")),
        "wardrobe": template.get("menu_label", template["id"]),
        "outfit": _outfit_summary(context["outfit"]),
        "location": context["interior"]["prompt"],
        "surface": _surface_summary(scene),
        "mood": context["mood"]["prompt"],
        "photography": scene["photography_style"]["prompt"],
        "pose": {"id": scene["pose"]["id"], "prompt": scene["pose"]["prompt"]},
        "action": {"id": scene["action"]["id"], "prompt": scene["action"]["prompt"]},
        "expression": {"id": scene["expression"]["id"], "prompt": scene["expression"]["prompt"]},
        "editorial_role": {"id": scene["editorial_role"]["id"], "prompt": scene["editorial_role"]["prompt"]},
        "camera": " · ".join(scene[key]["prompt"] for key in ("shot_size", "camera_angle", "framing", "focus_target")),
        "shot_size": {"id": scene["shot_size"]["id"], "prompt": scene["shot_size"]["prompt"]},
        "camera_angle": {"id": scene["camera_angle"]["id"], "prompt": scene["camera_angle"]["prompt"]},
        "framing": {"id": scene["framing"]["id"], "prompt": scene["framing"]["prompt"]},
        "focus_target": {"id": scene["focus_target"]["id"], "prompt": scene["focus_target"]["prompt"]},
        "explicit_recipe": ({"id": scene["explicit_recipe"]["id"], "prompt": scene["explicit_recipe"]["prompt"]} if scene.get("explicit_recipe") else None),
        "intimate_arousal_modifier": (
            {
                "id": scene["intimate_arousal_modifier"]["id"],
                "prompt": scene["intimate_arousal_modifier"]["prompt"],
            }
            if scene.get("intimate_arousal_modifier") else None
        ),
        "intensity": scene["intensity"],
        "garment_transition": scene.get("garment_transition", {}).get("prompt"),
        "positive_prompt": positive,
        "negative_prompt": negative,
        "prompt_warnings": prompt_lint(scene, positive),
        "selected_ids": selected_ids,
    }


STORYBOARD_FORMAT = "valhalla-storyboard"
STORYBOARD_FORMAT_VERSION = 1


def database_fingerprint(db: dict[str, Any]) -> str:
    canonical = json.dumps(
        db, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def encode_database_refs(value: Any, index: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        item_id = value.get("id")
        if isinstance(item_id, str) and index.get(item_id) == value:
            return {"$": item_id}
        return {key: encode_database_refs(item, index) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_database_refs(item, index) for item in value]
    return value


def decode_database_refs(value: Any, index: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$"}:
            item_id = value["$"]
            if not isinstance(item_id, str) or item_id not in index:
                raise AppError(f"Storyboard references unknown database item: {item_id}")
            return index[item_id]
        return {key: decode_database_refs(item, index) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_database_refs(item, index) for item in value]
    return value


DIRECTOR_HUMAN_GROUPS = (
    ("Identity", ("age", "ethnic_appearance", "skin_tone", "skin_marking")),
    ("Face", ("face_shape", "eye_shape", "eye_color", "eyebrows", "nose", "lips", "cheekbones", "jawline", "facial_accents")),
    ("Hair", ("hair_texture", "hair_length", "hair_style", "hair_color")),
    ("Body", ("height", "body_frame", "body_state", "waist", "hips", "breast_size", "breast_shape", "areola_size", "areola_color", "nipple_size", "nipple_shape", "pubic_hair", "genital_appearance")),
    ("Styling", ("makeup", "manicure")),
)

DIRECTOR_LABELS = {
    "age": "Age", "ethnic_appearance": "Nationality / appearance", "skin_tone": "Skin tone",
    "skin_marking": "Tan lines",
    "face_shape": "Face shape", "eye_shape": "Eye shape", "eye_color": "Eye color",
    "eyebrows": "Eyebrows", "nose": "Nose", "lips": "Lips", "cheekbones": "Cheekbones",
    "jawline": "Jawline", "facial_accents": "Facial detail", "hair_texture": "Hair texture",
    "hair_length": "Hair length", "hair_style": "Hair style", "hair_color": "Hair color",
    "height": "Height", "body_frame": "Body type", "body_state": "Body modifier", "waist": "Waist", "hips": "Hips",
    "breast_size": "Breast size", "breast_shape": "Breast shape",
    "areola_size": "Areola size", "areola_color": "Areola color",
    "nipple_size": "Nipple size", "nipple_shape": "Nipple shape",
    "pubic_hair": "Pubic hair", "genital_appearance": "Vulva appearance",
    "makeup": "Makeup", "manicure": "Manicure",
}


def director_stage_options(
    db: dict[str, Any], shot: dict[str, Any], content_mode: str
) -> list[dict[str, Any]]:
    template = shot["context"]["outfit"]["template"]
    effective = effective_photoshoot_stages(template)
    if content_mode == "sfw":
        stages = [copy.deepcopy(stage) for stage in effective if is_sfw_stage(stage)]
        for stage in stages:
            stage["sfw"] = True
        return stages
    unique: dict[str, dict[str, Any]] = {}
    for stage in effective:
        unique[stage["id"]] = stage
    reveal_rule = db["settings"]["dressed_panties_reveal"]
    compatible_ids = set(reveal_rule["compatible_outer_ids"])
    outfit = shot["context"]["outfit"]
    reveal_base = next((
        stage for stage in effective
        if stage["level"] == "covered"
        and "panties" in outfit["garments"]
        and any(
            outfit["garments"].get(slot, {}).get("id") in compatible_ids
            and slot in stage.get("visible_slots", [])
            for slot in reveal_rule["outer_slots"]
        )
    ), None)
    if reveal_base:
        reveal = dressed_panties_reveal_stage(reveal_base, outfit)
        unique[reveal["id"]] = reveal
    explicit_base = next(stage for stage in effective if stage["level"] == "explicit")
    kinds = ["provocative_rear", "intimate_closeup", "masturbation"]
    if "panties" in template.get("slots", {}):
        kinds.insert(2, "panties_aside")
    for kind in kinds:
        stage = copy.deepcopy(explicit_base)
        stage["id"] = f"{explicit_base['id']}_director_{kind}"
        stage["plateau_kind"] = kind
        stage["visible_slots"] = (
            [
                slot for slot in ("panties", "legwear", "footwear", "accessories")
                if slot in template.get("slots", {})
            ]
            if kind == "panties_aside" else []
        )
        stage["body_visibility"] = ["breasts", "nipples", "pubic_area", "genitals"]
        unique[stage["id"]] = stage
    return list(unique.values())


def director_option(item: dict[str, Any], current: str | None, defaults: set[str] | None = None) -> dict[str, Any]:
    return {
        "id": item["id"],
        "label": item.get("menu_label", item.get("prompt", item["id"])),
        "prompt": item.get("prompt", ""),
        "current": item["id"] == current,
        "default": item["id"] in (defaults or set()),
    }


def director_prop_options(db: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, Any]]:
    scene = shot["scene"]
    stage = shot["stage"]
    available = (
        set(stage.get("body_visibility", []))
        | set(stage.get("visible_slots", []))
        | {stage["level"]}
        | tags(scene["interior"])
        | tags(scene["furniture"])
        | tags(scene["action"])
    )
    if stage.get("visual_category"):
        available.add(stage["visual_category"])
    visible_slots = set(stage.get("visible_slots", []))
    available |= set().union(*(
        tags(item) for slot, item in scene["outfit"]["garments"].items()
        if slot in visible_slots
    ), set())
    required = set(scene["action"].get("requires_prop_tags", []))
    return [
        item for item in db["props"]
        if not item.get("disabled", False)
        and compatible_with_requirements(item, available)
        and (not required or required.issubset(tags(item)))
        and hands_required(scene["pose"]) + hands_required(scene["action"]) + hands_required(item) <= 2
    ]


def director_transition_options(scene: dict[str, Any]) -> list[dict[str, Any]]:
    transition = scene.get("garment_transition")
    if not transition:
        return []
    slots = transition.get("slots", [])
    names = [
        scene["outfit"]["garments"][slot]["prompt"]
        for slot in slots if slot in scene["outfit"]["garments"]
    ]
    if not names:
        return []
    garments = " and ".join(names)
    suffix = "_".join(slots)
    return [
        {
            "id": f"transition_{suffix}",
            "prompt": f"deliberately removing {garments}",
            "menu_label": f"Removing {garments}",
        },
        {
            "id": f"transition_in_motion_{suffix}",
            "prompt": f"in the act of taking off {garments}",
            "menu_label": f"Taking off {garments}",
        },
        {
            "id": f"transition_holding_removed_{suffix}",
            "prompt": f"holding the removed {garments} in one hand",
            "menu_label": f"Holding removed {garments}",
        },
    ]


def director_required_overrides(
    db: dict[str, Any], category: str, item: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    category_by_id = {
        candidate["id"]: candidate_category
        for candidate_category, values in db["human_model_parts"].items()
        for candidate in values
    }
    enabled = [
        candidate
        for values in db["human_model_parts"].values()
        for candidate in values
        if not candidate.get("disabled", False)
    ]
    selected = {category: item}
    for _ in range(len(HUMAN_SELECTION_ORDER) * 3):
        available = set().union(*(tags(candidate) for candidate in selected.values()))
        missing_groups: list[set[str]] = []
        for candidate in selected.values():
            required = set(candidate.get("requires_tags", [])) - available
            missing_groups.extend({tag} for tag in required)
            required_any = set(candidate.get("requires_any_tags", []))
            if required_any and not required_any & available:
                missing_groups.append(required_any)
        if not missing_groups:
            return selected
        requirement = missing_groups[0]
        providers = [
            candidate for candidate in enabled
            if tags(candidate) & requirement
            and category_by_id[candidate["id"]] != category
        ]
        if not providers:
            raise AppError(
                f"No human trait provides required tags {sorted(requirement)}"
            )
        provider = providers[0]
        selected[category_by_id[provider["id"]]] = provider
    raise AppError(f"Could not resolve prerequisites for {item['id']}")


class WebState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.storyboards: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.previews: dict[str, dict[str, Any]] = {}
        self._job_worker_running = False
        self._render_timings: dict[tuple[bool, str], list[float]] = {}

    def trim(self, mapping: dict[str, Any], maximum: int) -> None:
        while len(mapping) > maximum:
            mapping.pop(next(iter(mapping)))

    def create_storyboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        db, _ = load_database()
        args = parse_run_config(payload, db)
        prompt_seed = args.prompt_seed if args.prompt_seed is not None else automatic_ui_seed()
        args.prompt_seed = prompt_seed
        rng = random.Random(prompt_seed)
        composer = Composer(db, rng, args.use_curated_defaults)
        progression = db["settings"].get("photoshoot_progression", {})
        nsfw = float(progression.get("nsfw_final_percent", 50) if args.nsfw_percent is None else args.nsfw_percent)
        plateau = float(progression.get("explicit_plateau_percent", 30) if args.plateau_percent is None else args.plateau_percent)
        shots = build_storyboard(args, db, composer, rng, nsfw, plateau)
        storyboard_id = uuid.uuid4().hex
        record = {
            "id": storyboard_id,
            "created_at": _iso_now(),
            "db": db,
            "args": args,
            "composer": composer,
            "rng": rng,
            "shots": shots,
            "director_edited": False,
        }
        with self.lock:
            self.storyboards[storyboard_id] = record
            self.trim(self.storyboards, load_config()[0]["limits"]["max_storyboards"])
        return self.storyboard_payload(record)

    def storyboard_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        args = record["args"]
        scenes = [shot["scene"] for shot in record["shots"]]
        comparisons = 0
        changes = 0
        for previous, current in zip(scenes, scenes[1:]):
            for key in ("pose", "action", "furniture", "shot_size", "camera_angle", "framing", "focus_target"):
                comparisons += 1
                changes += previous[key]["id"] != current[key]["id"]
        return {
            "id": record["id"],
            "created_at": record["created_at"],
            "config": _args_dict(args),
            "total": len(record["shots"]),
            "diversity": round(changes * 100 / comparisons) if comparisons else 100,
            "director_edited": bool(record.get("director_edited", False)),
            "shots": [serialize_shot(record["db"], shot) for shot in record["shots"]],
        }

    def get_storyboard(self, storyboard_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.storyboards.get(storyboard_id)
        if record is None:
            raise AppError("Storyboard not found or expired")
        return record


    @staticmethod
    def _apply_director_customs(shot: dict[str, Any], scene: dict[str, Any], context: dict[str, Any]) -> None:
        merged = dict(context.get("custom_values", {}))
        merged.update(shot.get("custom_values", {}))
        if merged:
            scene["custom_values"] = merged
        else:
            scene.pop("custom_values", None)

    def director_payload(self, storyboard_id: str, number: int = 1) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        if not 1 <= number <= len(record["shots"]):
            raise AppError("Shot number is out of range")
        shot = record["shots"][number - 1]
        db = record["db"]
        context = shot["context"]
        human_defaults = (
            db["settings"].get("human_defaults", {}).get("pools", {})
            if record["composer"].use_curated_defaults else {}
        )
        groups = []
        for group_name, categories in DIRECTOR_HUMAN_GROUPS:
            fields = []
            for category in categories:
                values = [
                    item for item in db["human_model_parts"][category]
                    if not item.get("disabled", False)
                ]
                selected = context["human"][category]
                if isinstance(selected, list):
                    current = selected[0]["id"] if selected else ""
                    options = [{
                        "id": "", "label": "None", "prompt": "",
                        "current": not selected, "default": False,
                    }]
                else:
                    current = selected["id"]
                    options = []
                options.extend(
                    director_option(item, current, set(human_defaults.get(category, [])))
                    for item in values
                )
                fields.append({
                    "key": f"human.{category}",
                    "label": DIRECTOR_LABELS[category],
                    "scope": "set",
                    "value": current,
                    "options": options,
                })
            groups.append({"id": group_name.lower(), "label": group_name, "fields": fields})

        template = context["outfit"]["template"]
        wardrobe_fields = [{
            "key": "outfit.template",
            "label": "Outfit recipe",
            "scope": "set",
            "value": template["id"],
            "options": [
                director_option(
                    item, template["id"],
                    {
                        candidate["id"] for candidate in db["outfit_templates"]
                        if (
                            not record["composer"].use_curated_defaults
                            or candidate["catalog_category"] in set(
                                db["settings"]["scene_defaults"]["wardrobe_categories"]
                            )
                        )
                    },
                )
                for item in db["outfit_templates"]
                if not item.get("disabled", False)
                and (
                    record["args"].content_mode != "sfw"
                    or template_supports_sfw(db, item)
                )
            ],
        }]
        for slot, rule in template["slots"].items():
            garment = context["outfit"]["garments"].get(slot)
            candidates = [
                item for item in db["garments"][rule["catalog"]]
                if not item.get("disabled", False)
                and garment_matches_template_slot(
                    db, template, slot, item, record["args"].content_mode
                )
                and set(item.get("requires_environment_tags", [])).issubset(tags(context["interior"]))
                and not set(item.get("excludes_environment_tags", [])) & tags(context["interior"])
                and (
                    record["args"].content_mode != "sfw"
                    or not (tags(item) & SFW_BLOCKED_GARMENT_TAGS)
                )
            ]
            layer_compatible = []
            for item in candidates:
                candidate_garments = dict(context["outfit"]["garments"])
                candidate_garments[slot] = item
                try:
                    validate_outfit_layers(
                        db,
                        {"template": template, "garments": candidate_garments},
                    )
                    candidate_outfit = {
                        "template": template,
                        "garments": candidate_garments,
                        "colors": dict(context["outfit"]["colors"]),
                    }
                    set_outfit_group_color(db, candidate_outfit, slot)
                    if record["args"].content_mode == "sfw":
                        validate_sfw_outfit({
                            "template": template,
                            "garments": candidate_garments,
                        })
                except AppError:
                    continue
                layer_compatible.append(item)
            candidates = layer_compatible
            garment_options = [
                director_option(item, garment["id"] if garment else None)
                for item in candidates
            ]
            if not rule.get("required", False):
                garment_options.insert(0, {
                    "id": "", "label": "None", "prompt": "",
                    "current": garment is None, "default": garment is None,
                })
            wardrobe_fields.append({
                "key": f"outfit.garments.{slot}",
                "label": slot.replace("_", " ").title(),
                "scope": "set",
                "value": garment["id"] if garment else "",
                "options": garment_options,
            })
            if not garment:
                continue
            color = context["outfit"]["colors"][slot]
            allowed_colors = set(
                garment.get("allowed_colors") or [item["id"] for item in db["colors"]]
            )
            color_group = rule.get("color_group")
            if color_group:
                for grouped_slot, grouped_rule in template["slots"].items():
                    grouped_garment = context["outfit"]["garments"].get(grouped_slot)
                    if (
                        grouped_garment
                        and grouped_rule.get("color_group") == color_group
                    ):
                        allowed_colors &= set(
                            grouped_garment.get("allowed_colors")
                            or [item["id"] for item in db["colors"]]
                        )
            wardrobe_fields.append({
                "key": f"outfit.colors.{slot}",
                "label": f"{slot.replace('_', ' ').title()} color",
                "scope": "set",
                "value": color["id"],
                "options": [
                    director_option(item, color["id"]) for item in db["colors"]
                    if item["id"] in allowed_colors and not item.get("disabled", False)
                ],
            })
            for section, key, label in (
                ("patterns", "patterns", "Pattern"),
                ("fabric_textures", "textures", "Texture"),
            ):
                current_item = context["outfit"].get(key, {}).get(slot)
                modifiers = [
                    item for item in db[section]
                    if not item.get("disabled", False)
                    and garment["id"] in item["allowed_garment_ids"]
                ]
                wardrobe_fields.append({
                    "key": f"outfit.{key}.{slot}",
                    "label": f"{slot.replace('_', ' ').title()} {label.lower()}",
                    "scope": "set",
                    "value": current_item["id"] if current_item else "",
                    "options": [{
                        "id": "", "label": "None / plain", "prompt": "",
                        "current": current_item is None, "default": True,
                    }] + [
                        director_option(item, current_item["id"] if current_item else None)
                        for item in modifiers
                    ],
                })
        groups.append({"id": "wardrobe", "label": "Wardrobe", "fields": wardrobe_fields})

        scene_defaults = (
            db["settings"]["scene_defaults"].get("pools", {})
            if record["composer"].use_curated_defaults else {}
        )
        scene_fields = []
        for key, section, label in (
            ("interior", "interiors", "Location"),
            ("mood", "moods", "Mood"),
            ("photography_style", "photography_styles", "Render style"),
        ):
            current_item = context[key]
            values = [item for item in db[section] if not item.get("disabled", False)]
            if key == "furniture":
                values = [
                    item for item in values
                    if compatible_with_requirements(item, tags(context["interior"]))
                ]
            scene_fields.append({
                "key": f"scene.{key}",
                "label": label,
                "scope": "set",
                "value": current_item["id"],
                "options": [
                    director_option(item, current_item["id"], set(scene_defaults.get(section, [])))
                    for item in values
                ],
            })
        groups.append({"id": "scene", "label": "Scene & treatment", "fields": scene_fields})

        camera_fields = []
        for key, section, label in (
            ("furniture", "furniture", "Surface / support"),
            ("editorial_role", "editorial_roles", "Editorial role"),
            ("shot_size", "shot_sizes", "Shot size"),
            ("camera_angle", "camera_angles", "Camera angle"),
            ("framing", "framings", "Framing"),
            ("focus_target", "focus_targets", "Focus target"),
        ):
            current_item = shot["scene"][key]
            camera_tags = (
                set(shot["stage"].get("body_visibility", []))
                | set(shot["stage"].get("visible_slots", []))
                | {shot["stage"]["level"]}
                | tags(shot["scene"]["interior"])
                | tags(shot["scene"]["furniture"])
                | tags(shot["scene"]["pose"])
                | tags(shot["scene"]["action"])
                | tags(shot["scene"]["editorial_role"])
                | tags(shot["scene"]["shot_size"])
                | tags(shot["scene"]["camera_angle"])
                | tags(shot["scene"]["framing"])
                | tags(shot["scene"]["focus_target"])
            )
            compatible = [
                item for item in db[section]
                if not item.get("disabled", False)
                and (
                    key == "furniture"
                    and compatible_with_requirements(item, tags(context["interior"]))
                    and category_allows(context["interior"], item)
                    or key == "editorial_role"
                    or key not in {"furniture", "editorial_role"}
                    and shot["stage"]["level"] in item.get("allowed_levels", [shot["stage"]["level"]])
                    and item_allows_intensity(item, shot["scene"]["intensity"])
                    and compatible_with_requirements(item, camera_tags)
                )
            ]
            if key in {"shot_size", "camera_angle", "framing", "focus_target"}:
                compatible = [
                    item for item in compatible
                    if camera_candidate_compatible(shot["scene"], key, item)
                ]
            camera_fields.append({
                "key": f"shot.{key}", "label": label, "scope": "shot",
                "value": current_item["id"],
                "options": [director_option(item, current_item["id"]) for item in compatible],
            })
        furniture = shot["scene"]["furniture"]
        for kind, label in (("color", "Surface color"), ("texture", "Surface texture")):
            candidates = surface_modifier_candidates(db, furniture, kind)
            if not candidates:
                continue
            key = f"surface_{kind}"
            current_item = shot["scene"].get(key)
            camera_fields.append({
                "key": f"shot.{key}", "label": label, "scope": "shot",
                "value": current_item["id"] if current_item else "",
                "options": [{
                    "id": "", "label": "None / original", "prompt": "",
                    "current": current_item is None, "default": current_item is None,
                }] + [
                    director_option(item, current_item["id"] if current_item else None)
                    for item in candidates
                ],
            })
        compatible_recipes = [
            item for item in db["explicit_recipes"]
            if not item.get("disabled", False)
            and (
                not shot["stage"].get("plateau_kind")
                or item.get("plateau_kind") == shot["stage"].get("plateau_kind")
            )
        ]
        if shot["stage"]["level"] == "explicit" and compatible_recipes:
            current_recipe = shot["scene"].get("explicit_recipe")
            camera_fields.append({
                "key": "shot.explicit_recipe", "label": "Explicit recipe", "scope": "shot",
                "value": current_recipe["id"] if current_recipe else "",
                "options": [
                    director_option(item, current_recipe["id"] if current_recipe else None)
                    for item in compatible_recipes
                ],
            })
        camera_fields.append({
            "key": "shot.intensity", "label": "Visual intensity", "scope": "shot",
            "value": shot["scene"]["intensity"],
            "options": [
                {
                    "id": level, "label": level.title(),
                    "prompt": f"{level} visual intensity",
                    "current": level == shot["scene"]["intensity"],
                    "default": level == shot["scene"]["intensity"],
                }
                for level in (
                    ("fashion", "sensual")
                    if record["args"].content_mode == "sfw"
                    else allowed_scene_intensities(shot["scene"])
                )
            ],
        })
        groups.append({"id": "camera", "label": "Camera & editorial", "fields": camera_fields})

        direction_fields = []
        stages = director_stage_options(db, shot, record["args"].content_mode)
        direction_fields.append({
            "key": "shot.stage", "label": "Stage / content", "scope": "shot",
            "value": shot["stage"]["id"],
            "options": [
                {
                    "id": item["id"],
                    "label": (item.get("plateau_kind") or item["level"]).replace("_", " ").title(),
                    "prompt": item["level"], "current": item["id"] == shot["stage"]["id"],
                    "default": item["id"] == shot["stage"]["id"],
                } for item in stages
            ],
        })
        for key, section, label in (
            ("pose", "poses", "Pose"),
            ("action", "actions", "Action"),
            ("expression", "expressions", "Expression"),
        ):
            current_item = shot["scene"][key]
            stage = shot["stage"]
            available = (
                set(stage.get("body_visibility", [])) | set(stage.get("visible_slots", []))
                | {stage["level"]} | tags(shot["scene"]["furniture"])
                | tags(shot["scene"]["interior"])
            )
            if stage.get("visual_category"):
                available.add(stage["visual_category"])
            visible_slots = set(stage.get("visible_slots", []))
            available |= set().union(*(
                tags(item) for slot, item in shot["scene"]["outfit"]["garments"].items()
                if slot in visible_slots
            ), set())
            compatible = [item for item in db[section] if not item.get("disabled", False)]
            if key == "pose":
                compatible = [
                    item for item in compatible
                    if stage["level"] in item.get("allowed_levels", [stage["level"]])
                    and item_allows_intensity(item, shot["scene"]["intensity"])
                    and compatible_with_requirements(item, available)
                ]
                if stage.get("sfw"):
                    compatible = [
                        item for item in compatible
                        if not (tags(item) & SFW_BLOCKED_DIRECTION_TAGS)
                    ]
                if stage["level"] == "explicit":
                    compatible = [item for item in compatible if "explicit_pose" in tags(item)]
                elif stage["level"] in {"topless", "nude"}:
                    compatible = [item for item in compatible if tags(item) & {"erotic_pose", "topless_pose", "nude_pose", "open_legs"}]
                plateau = stage.get("plateau_kind")
                plateau_tags = {
                    "provocative_rear": {"provocative_rear"},
                    "intimate_closeup": {"intimate_closeup"},
                    "masturbation": {"masturbation_pose"},
                    "panties_aside": {"open_legs"},
                }.get(plateau)
                if plateau_tags:
                    compatible = [item for item in compatible if tags(item) & plateau_tags]
                recipe = shot["scene"].get("explicit_recipe")
                if recipe and recipe.get("pose_tags"):
                    recipe_tags = set(recipe["pose_tags"])
                    compatible = [item for item in compatible if recipe_tags.issubset(tags(item))]
                compatible = [
                    item for item in compatible
                    if recipe_focus_compatible(item, recipe, "pose")
                ]
            elif key == "action":
                outfit_tags = set().union(*(
                    tags(item) for item in shot["scene"]["outfit"]["garments"].values()
                ), set())
                action_tags = available | outfit_tags | tags(shot["scene"]["pose"])
                compatible = [
                    item for item in compatible
                    if stage["level"] in item.get("allowed_levels", [stage["level"]])
                    and item_allows_intensity(item, shot["scene"]["intensity"])
                    and compatible_with_requirements(item, action_tags)
                    and hands_required(shot["scene"]["pose"]) + hands_required(item) <= 2
                ]
                if stage.get("sfw"):
                    compatible = [
                        item for item in compatible
                        if not (tags(item) & SFW_BLOCKED_DIRECTION_TAGS)
                    ]
                if stage["level"] == "explicit":
                    compatible = [item for item in compatible if "explicit_action" in tags(item)]
                elif stage["level"] in {"topless", "nude"}:
                    compatible = [item for item in compatible if tags(item) & {"erotic_action", "undressing_action"}]
                plateau = stage.get("plateau_kind")
                plateau_tags = {
                    "provocative_rear": {"provocative_action"},
                    "intimate_closeup": {"closeup_action"},
                    "masturbation": {"masturbation_action"},
                    "panties_aside": {"panties_aside_action"},
                }.get(plateau)
                if plateau_tags:
                    compatible = [item for item in compatible if tags(item) & plateau_tags]
                recipe = shot["scene"].get("explicit_recipe")
                if recipe and recipe.get("action_tags"):
                    recipe_tags = set(recipe["action_tags"])
                    compatible = [item for item in compatible if recipe_tags.issubset(tags(item))]
                compatible = [
                    item for item in compatible
                    if recipe_focus_compatible(item, recipe, "action")
                ]
            else:
                compatible = [
                    item for item in compatible
                    if item_allows_intensity(item, shot["scene"]["intensity"])
                ]
                required = set(shot["scene"]["action"].get("requires_expression_tags", []))
                if required:
                    compatible = [item for item in compatible if required.issubset(tags(item))]
                    if stage["level"] == "lingerie":
                        subtle = [
                            item for item in compatible
                            if item["id"] == "expression_shy_sultry"
                        ]
                        compatible = subtle or compatible
                elif stage["level"] in {"covered", "lingerie"}:
                    natural_expressions = {
                        "expression_confident", "expression_soft_smile",
                        "expression_dreamy", "expression_playful",
                        "expression_serene", "expression_shy_sultry",
                    }
                    compatible = [
                        item for item in compatible
                        if item["id"] in natural_expressions
                    ]
                elif stage["level"] in {"topless", "nude"}:
                    compatible = [
                        item for item in compatible
                        if not tags(item) & {
                            "pleasure_expression", "intense_pleasure_expression",
                        }
                    ]
            direction_fields.append({
                "key": f"shot.{key}", "label": label, "scope": "shot",
                "value": current_item["id"],
                "options": [director_option(item, current_item["id"]) for item in compatible],
            })
        current_prop = shot["scene"].get("prop")
        prop_candidates = director_prop_options(db, shot)
        prop_required = bool(
            shot["scene"]["action"].get("requires_prop_tags", [])
        )
        direction_fields.append({
            "key": "shot.prop", "label": "Prop", "scope": "shot",
            "value": current_prop["id"] if current_prop else "",
            "options": ([] if prop_required else [{
                "id": "", "label": "None", "prompt": "",
                "current": current_prop is None, "default": current_prop is None,
            }]) + [
                director_option(item, current_prop["id"] if current_prop else None)
                for item in prop_candidates
            ],
        })
        transition_candidates = director_transition_options(shot["scene"])
        if transition_candidates:
            current_transition = shot["scene"]["garment_transition"]
            direction_fields.append({
                "key": "shot.garment_transition",
                "label": "Garment transition",
                "scope": "shot",
                "value": current_transition["id"] if current_transition.get("prompt") else "",
                "options": [{
                    "id": "", "label": "None / no removal action", "prompt": "",
                    "current": False, "default": False,
                }] + [
                    director_option(
                        item,
                        current_transition["id"] if current_transition.get("prompt") else None,
                    )
                    for item in transition_candidates
                ],
            })
        direction_by_key = {field["key"]: field for field in direction_fields}
        direction_by_key["shot.stage"]["compatibility"] = {
            "poses": len(direction_by_key["shot.pose"]["options"]),
            "actions": len(direction_by_key["shot.action"]["options"]),
            "expressions": len(direction_by_key["shot.expression"]["options"]),
        }
        groups.append({"id": "direction", "label": "Shot direction", "fields": direction_fields})
        custom_values = dict(context.get("custom_values", {}))
        custom_values.update(shot.get("custom_values", {}))
        for group in groups:
            for director_field in group["fields"]:
                director_field["custom"] = custom_values.get(director_field["key"], "")
        return {
            "storyboard_id": storyboard_id,
            "shot": number,
            "total": len(record["shots"]),
            "photoshoot_index": shot["photoshoot_index"],
            "shot_index": shot["shot_index"],
            "summary": serialize_shot(db, shot),
            "groups": groups,
        }

    def _replace_director_context(
        self,
        record: dict[str, Any],
        shot_position: int,
        context: dict[str, Any],
        recalculate_stages: bool = False,
        preserve_photography: bool = False,
        manual_field: str | None = None,
    ) -> None:
        args = record["args"]
        db = record["db"]
        source = record["shots"][shot_position]
        indices = [shot_position] if args.mode == "random" else [
            index for index, shot in enumerate(record["shots"])
            if shot["photoshoot_index"] == source["photoshoot_index"]
        ]
        progression = db["settings"].get("photoshoot_progression", {})
        nsfw = float(progression.get("nsfw_final_percent", 50) if args.nsfw_percent is None else args.nsfw_percent)
        plateau = float(progression.get("explicit_plateau_percent", 30) if args.plateau_percent is None else args.plateau_percent)
        xxx_fallback_plan = (
            full_xxx_recipe_plan(db, args.count, record["rng"])
            if (
                recalculate_stages
                and args.content_mode == "xxx"
                and any(
                    not record["shots"][index]["stage"].get("planned_recipe_id")
                    for index in indices
                )
            ) else []
        )
        recipes_by_id = {item["id"]: item for item in db["explicit_recipes"]}
        replacements = []
        for index in indices:
            old = record["shots"][index]
            stage = old["stage"]
            if recalculate_stages:
                stage = (
                    sfw_stage(context["outfit"]["template"], old["shot_index"], args.count, args.mode, record["rng"])
                    if args.content_mode == "sfw"
                    else (
                        full_xxx_stage(
                            context["outfit"]["template"],
                            recipes_by_id.get(old["stage"].get("planned_recipe_id"))
                            or xxx_fallback_plan[old["shot_index"]],
                            old["shot_index"],
                        )
                        if args.content_mode == "xxx"
                        else stage_for_index(
                            context["outfit"]["template"], old["shot_index"], args.count,
                            args.mode, record["rng"], nsfw, plateau,
                        )
                    )
                )
            scene_overrides = {}
            if stage.get("planned_recipe_id"):
                scene_overrides = {
                    "explicit_recipe": stage["planned_recipe_id"],
                    "intensity": stage.get("planned_intensity", "explicit"),
                }
            scene = record["composer"].resolve_scene(
                context, stage, scene_overrides
            )
            self._apply_director_customs(old, scene, context)
            if preserve_photography:
                scene["photography_style"] = context["photography_style"]
                scene["dependencies"] = record["composer"].resolve_dependencies(scene)
                record["composer"].validate_scene_rules(scene)
            replacements.append((index, stage, scene))
        for index, stage, scene in replacements:
            record["shots"][index]["context"] = context
            record["shots"][index]["stage"] = stage
            record["shots"][index]["scene"] = scene
            if manual_field:
                fields = set(record["shots"][index].get("manual_fields", []))
                fields.add(manual_field)
                record["shots"][index]["manual_fields"] = sorted(fields)
            if recalculate_stages:
                record["shots"][index]["stage_manual"] = False

    def update_director(self, storyboard_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        with self.lock:
            active = any(
                job["storyboard_id"] == storyboard_id
                and job["status"] in {"queued", "running"}
                for job in self.jobs.values()
            )
        if active:
            raise AppError("Director editing is unavailable while this storyboard is rendering")
        record["director_edited"] = True
        number = _safe_int(payload.get("shot"), "Shot", 1, len(record["shots"]))
        field = str(payload.get("field", ""))
        value = str(payload.get("value", ""))
        position = number - 1
        shot = record["shots"][position]
        db = record["db"]
        index = {item["id"]: item for item in iter_content_items(db)}
        context = copy.deepcopy(shot["context"])
        recalculate_stages = False
        preserve_photography = False
        clear_custom = payload.get("clear_custom") is True
        if clear_custom and not field.startswith("shot."):
            context.setdefault("custom_values", {}).pop(field, None)

        if value == "__director_random__" and "custom_value" not in payload:
            director = self.director_payload(storyboard_id, number)
            director_field = next(
                (
                    candidate
                    for group in director["groups"]
                    for candidate in group["fields"]
                    if candidate["key"] == field
                ),
                None,
            )
            if director_field is None:
                raise AppError("Unknown Director field")
            candidates = [
                option["id"] for option in director_field["options"]
                if option.get("id")
            ]
            alternatives = [
                candidate for candidate in candidates
                if candidate != director_field.get("value")
            ]
            if alternatives:
                candidates = alternatives
            if not candidates:
                raise AppError("This Director field has no randomizable values")
            value = record["rng"].choice(candidates)

        if "custom_value" in payload:
            custom_value = payload.get("custom_value")
            if not isinstance(custom_value, str):
                raise AppError("Custom value must be text")
            custom_value = custom_value.strip()
            if len(custom_value) > 600:
                raise AppError("Custom value cannot exceed 600 characters")
            custom_scope = payload.get("custom_scope", "current")
            if custom_scope not in {"current", "all"}:
                raise AppError("Custom scope must be current or all")
            parts = field.split(".")
            valid = (
                (len(parts) == 2 and parts[0] == "human" and parts[1] in db["human_model_parts"])
                or field == "outfit.template"
                or (
                    len(parts) == 3 and parts[0] == "outfit"
                    and parts[1] in {"garments", "colors", "patterns", "textures"}
                    and parts[2] in context["outfit"]["template"]["slots"]
                )
                or field in {"scene.interior", "scene.furniture", "scene.mood", "scene.photography_style"}
                or field in {
                    "shot.stage", "shot.pose", "shot.action", "shot.expression",
                    "shot.prop",
                    "shot.furniture", "shot.editorial_role", "shot.shot_size",
                    "shot.camera_angle", "shot.framing", "shot.focus_target",
                    "shot.surface_color", "shot.surface_texture",
                    "shot.explicit_recipe", "shot.intensity", "shot.garment_transition",
                }
            )
            if not valid:
                raise AppError("Unknown Director field")
            if custom_scope == "all" and field.startswith("shot."):
                raise AppError("Shot-scoped custom values cannot be applied to all sets")
            if not field.startswith("shot."):
                if custom_scope == "all":
                    if record["args"].mode == "random":
                        targets = range(len(record["shots"]))
                    else:
                        seen_sets = set()
                        targets = []
                        for target_position, candidate in enumerate(record["shots"]):
                            set_index = candidate["photoshoot_index"]
                            if set_index not in seen_sets:
                                seen_sets.add(set_index)
                                targets.append(target_position)
                    for target_position in targets:
                        target_context = (
                            context
                            if target_position == position
                            else copy.deepcopy(record["shots"][target_position]["context"])
                        )
                        target_custom_values = target_context.setdefault("custom_values", {})
                        if custom_value:
                            target_custom_values[field] = custom_value
                        else:
                            target_custom_values.pop(field, None)
                        self._replace_director_context(
                            record, target_position, target_context, manual_field=field
                        )
                else:
                    custom_values = context.setdefault("custom_values", {})
                    if custom_value:
                        custom_values[field] = custom_value
                    else:
                        custom_values.pop(field, None)
                    self._replace_director_context(
                        record, position, context, manual_field=field
                    )
            else:
                custom_values = shot.setdefault("custom_values", {})
                if custom_value:
                    custom_values[field] = custom_value
                else:
                    custom_values.pop(field, None)
                self._apply_director_customs(shot, shot["scene"], shot["context"])
            fields = set(shot.get("manual_fields", []))
            fields.add(field)
            shot["manual_fields"] = sorted(fields)
            return self.director_payload(storyboard_id, number)

        if field.startswith("remix."):
            target = field.split(".", 1)[1]
            if target == "shot":
                shot["scene"] = record["composer"].resolve_scene(
                    context, shot["stage"]
                )
                self._apply_director_customs(shot, shot["scene"], context)
                return self.director_payload(storyboard_id, number)
            if target == "subject":
                context["human"] = record["composer"].choose_human(
                    use_default_ethnicity=False, use_human_defaults=False
                )
            elif target == "wardrobe":
                context["outfit"] = record["composer"].choose_outfit(
                    context["outfit"]["template"], context["interior"],
                    record["args"].content_mode,
                )
            elif target == "scene":
                interiors = [
                    item for item in db["interiors"]
                    if not item.get("disabled", False)
                    and (
                        not record["composer"].use_curated_defaults
                        or catalog_category(item) in set(
                            db["settings"]["scene_defaults"]["environment_categories"]
                        )
                    )
                ]
                context["interior"] = weighted_choice(record["rng"], interiors)
                furniture = [
                    item for item in db["furniture"]
                    if not item.get("disabled", False)
                    and compatible_with_requirements(item, tags(context["interior"]))
                    and category_allows(context["interior"], item)
                ]
                context["furniture"] = weighted_choice(record["rng"], furniture)
                context["mood"] = weighted_choice(
                    record["rng"],
                    [item for item in db["moods"] if not item.get("disabled", False)],
                )
                context["photography_style"] = weighted_choice(
                    record["rng"],
                    [
                        item for item in db["photography_styles"]
                        if not item.get("disabled", False)
                    ],
                )
                try:
                    record["composer"].validate_outfit_environment(
                        context["outfit"], context["interior"]
                    )
                except AppError:
                    context["outfit"] = record["composer"].choose_outfit(
                        context["outfit"]["template"], context["interior"],
                        record["args"].content_mode,
                    )
                preserve_photography = True
            else:
                raise AppError("Unknown remix action")
        elif field.startswith("human."):
            category = field.split(".", 1)[1]
            if category not in db["human_model_parts"]:
                raise AppError("Unknown human trait")
            if category == "facial_accents":
                if value:
                    item = index.get(value)
                    if item not in db["human_model_parts"][category]:
                        raise AppError("Unknown facial detail")
                    context["human"][category] = [item]
                else:
                    context["human"][category] = []
            else:
                item = index.get(value)
                if item not in db["human_model_parts"][category]:
                    raise AppError("Unknown human trait value")
                overrides = {
                    key: selected for key, selected in context["human"].items()
                    if key != "facial_accents" and isinstance(selected, dict)
                }
                overrides[category] = item
                try:
                    human = record["composer"].choose_human(
                        overrides, use_human_defaults=False
                    )
                except AppError:
                    required_overrides = director_required_overrides(
                        db, category, item
                    )
                    human = record["composer"].choose_human(
                        required_overrides, use_human_defaults=False
                    )
                human["facial_accents"] = [
                    accent for accent in context["human"].get("facial_accents", [])
                    if compatible_with_requirements(
                        accent, set().union(*(tags(part) for part in human.values() if isinstance(part, dict)))
                    )
                ]
                context["human"] = human
        elif field == "outfit.template":
            template = index.get(value)
            if template not in db["outfit_templates"]:
                raise AppError("Unknown outfit recipe")
            if (
                record["args"].content_mode == "sfw"
                and not template_supports_sfw(db, template)
            ):
                raise AppError("This outfit recipe has no SFW-compatible covered stage")
            context["outfit"] = record["composer"].choose_outfit(
                template, context["interior"], record["args"].content_mode
            )
            recalculate_stages = True
        elif field.startswith("outfit."):
            _, section, slot = field.split(".", 2)
            outfit = context["outfit"]
            if slot not in outfit["template"]["slots"]:
                raise AppError("This outfit does not define that slot")
            if section == "garments":
                rule = outfit["template"]["slots"][slot]
                if not value:
                    if rule.get("required", False):
                        raise AppError("A required garment cannot be removed")
                    outfit["garments"].pop(slot, None)
                    outfit["colors"].pop(slot, None)
                    outfit.get("patterns", {}).pop(slot, None)
                    outfit.get("textures", {}).pop(slot, None)
                else:
                    garment = index.get(value)
                    if not garment or not garment_matches_template_slot(
                        db, outfit["template"], slot, garment,
                        record["args"].content_mode,
                    ):
                        raise AppError("Garment is incompatible with this outfit slot")
                    outfit["garments"][slot] = garment
                    allowed = [
                        color for color in db["colors"]
                        if color["id"] in set(
                            garment.get("allowed_colors")
                            or [item["id"] for item in db["colors"]]
                        )
                    ]
                    if outfit["colors"].get(slot) not in allowed:
                        outfit["colors"][slot] = allowed[0]
                    set_outfit_group_color(db, outfit, slot)
                    for modifier_key in ("patterns", "textures"):
                        modifier = outfit.get(modifier_key, {}).get(slot)
                        if modifier and garment["id"] not in modifier["allowed_garment_ids"]:
                            outfit[modifier_key].pop(slot, None)
            elif section == "colors":
                color = index.get(value)
                if color not in db["colors"]:
                    raise AppError("Color is incompatible with this garment")
                set_outfit_group_color(db, outfit, slot, color["id"])
            elif section in {"patterns", "textures"}:
                source = "patterns" if section == "patterns" else "fabric_textures"
                if not value:
                    outfit.setdefault(section, {}).pop(slot, None)
                else:
                    modifier = index.get(value)
                    if modifier not in db[source] or outfit["garments"][slot]["id"] not in modifier["allowed_garment_ids"]:
                        raise AppError("Modifier is incompatible with this garment")
                    outfit.setdefault(section, {})[slot] = modifier
            else:
                raise AppError("Unknown wardrobe field")
            validate_outfit_layers(db, outfit)
            validate_outfit_color_groups(outfit)
            record["composer"].validate_outfit_stage_coverage(outfit)
            if record["args"].content_mode == "sfw":
                validate_sfw_outfit(outfit)
            record["composer"].validate_outfit_environment(outfit, context["interior"])
        elif field.startswith("scene."):
            key = field.split(".", 1)[1]
            sections = {
                "interior": "interiors", "furniture": "furniture",
                "mood": "moods", "photography_style": "photography_styles",
            }
            if key not in sections or index.get(value) not in db[sections[key]]:
                raise AppError("Unknown scene selection")
            context[key] = index[value]
            if key == "interior":
                furniture = [
                    item for item in db["furniture"]
                    if not item.get("disabled", False)
                    and compatible_with_requirements(item, tags(context["interior"]))
                    and category_allows(context["interior"], item)
                ]
                if context["furniture"] not in furniture:
                    context["furniture"] = weighted_choice(record["rng"], furniture)
                try:
                    record["composer"].validate_outfit_environment(context["outfit"], context["interior"])
                except AppError:
                    context["outfit"] = record["composer"].choose_outfit(
                        context["outfit"]["template"], context["interior"],
                        record["args"].content_mode,
                    )
            preserve_photography = key == "photography_style"
        elif field.startswith("shot."):
            key = field.split(".", 1)[1]
            if key == "stage":
                stages = director_stage_options(db, shot, record["args"].content_mode)
                stage = next((item for item in stages if item["id"] == value), None)
                if stage is None:
                    raise AppError("Unknown stage")
                scene = record["composer"].resolve_scene(context, stage)
                shot["stage"], shot["scene"] = stage, scene
                shot["stage_manual"] = True
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            elif key == "intensity":
                if value not in INTENSITY_LEVELS:
                    raise AppError("Unknown intensity")
                if record["args"].content_mode == "sfw" and value not in {"fashion", "sensual"}:
                    raise AppError("SFW only storyboards allow fashion or sensual intensity")
                if value not in allowed_scene_intensities(shot["scene"]):
                    raise AppError(
                        f"Intensity {value} is incompatible with this stage and recipe"
                    )
                shot["scene"]["intensity"] = value
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            elif key in {"surface_color", "surface_texture"}:
                kind = key.removeprefix("surface_")
                candidates = surface_modifier_candidates(
                    db, shot["scene"]["furniture"], kind
                )
                selected = next(
                    (item for item in candidates if item["id"] == value), None
                ) if value else None
                if value and selected is None:
                    raise AppError(f"Surface {kind} is incompatible with this surface")
                shot["scene"][key] = selected
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            elif key == "prop":
                candidates = director_prop_options(db, shot)
                selected = next(
                    (item for item in candidates if item["id"] == value), None
                ) if value else None
                required = bool(
                    shot["scene"]["action"].get("requires_prop_tags", [])
                )
                if value and selected is None:
                    raise AppError("Prop is incompatible with this shot")
                if not selected and required:
                    raise AppError("This action requires a compatible prop")
                shot["scene"]["prop"] = selected
                shot["scene"]["dependencies"] = record["composer"].resolve_dependencies(
                    shot["scene"]
                )
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            elif key == "garment_transition":
                options = director_transition_options(shot["scene"])
                slots = list(
                    shot["scene"].get("garment_transition", {}).get("slots", [])
                )
                selected = next(
                    (item for item in options if item["id"] == value), None
                ) if value else None
                if value and selected is None:
                    raise AppError("Garment transition is incompatible with this shot")
                if selected:
                    shot["scene"]["garment_transition"] = {
                        "id": selected["id"],
                        "prompt": selected["prompt"],
                        "slots": slots,
                    }
                else:
                    shot["scene"]["garment_transition"] = {
                        "id": f"transition_none_{'_'.join(slots)}",
                        "prompt": "",
                        "slots": slots,
                    }
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            elif key in {
                "pose", "action", "expression", "furniture", "editorial_role",
                "shot_size", "camera_angle", "framing", "focus_target", "explicit_recipe",
            }:
                if value not in index:
                    raise AppError("Unknown direction")
                preserved = {
                    candidate: shot["scene"][candidate]["id"]
                    for candidate in (
                        "pose", "action", "expression", "furniture", "editorial_role",
                        "shot_size", "camera_angle", "framing", "focus_target", "explicit_recipe",
                        "surface_color", "surface_texture",
                    )
                    if candidate != key and shot["scene"].get(candidate)
                }
                preserved["prop"] = (
                    shot["scene"]["prop"]["id"]
                    if shot["scene"].get("prop") else ""
                )
                preserved[key] = value
                previous_transition = copy.deepcopy(
                    shot["scene"].get("garment_transition")
                )
                try:
                    scene = record["composer"].resolve_scene(
                        context, shot["stage"], preserved
                    )
                except AppError:
                    scene = record["composer"].resolve_scene(
                        context, shot["stage"], {key: value}
                    )
                shot["scene"] = scene
                if previous_transition:
                    shot["scene"]["garment_transition"] = previous_transition
                if clear_custom:
                    shot.setdefault("custom_values", {}).pop(field, None)
                self._apply_director_customs(shot, shot["scene"], context)
            else:
                raise AppError("Unknown shot field")
            return self.director_payload(storyboard_id, number)
        else:
            raise AppError("Unknown Director field")

        self._replace_director_context(
            record, position, context, recalculate_stages, preserve_photography,
            manual_field=field,
        )
        return self.director_payload(storyboard_id, number)

    def export_storyboard(self, storyboard_id: str) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        db = record["db"]
        index = {item["id"]: item for item in iter_content_items(db)}
        shots = []
        for shot in record["shots"]:
            context = shot["context"]
            scene_delta = {
                key: value for key, value in shot["scene"].items()
                if key not in context or context[key] != value
            }
            shots.append({
                "n": shot["number"],
                "p": shot["photoshoot_index"],
                "s": shot["shot_index"],
                "seed": shot["inference_seed"],
                "stage": encode_database_refs(shot["stage"], index),
                "stage_manual": bool(shot.get("stage_manual", False)),
                "manual_fields": sorted(set(shot.get("manual_fields", []))),
                "context": encode_database_refs(context, index),
                "scene": encode_database_refs(scene_delta, index),
                "custom": shot.get("custom_values", {}),
            })
        return {
            "format": STORYBOARD_FORMAT,
            "version": STORYBOARD_FORMAT_VERSION,
            "database": database_fingerprint(db),
            "created_at": record["created_at"],
            "director_edited": bool(record.get("director_edited", False)),
            "config": _args_dict(record["args"]),
            "shots": shots,
        }

    def import_storyboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("format") != STORYBOARD_FORMAT:
            if all(key in payload for key in ("id", "config", "shots")):
                raise AppError(
                    "This file is a UI storyboard snapshot, not an export file. "
                    "Export the storyboard again with the updated server."
                )
            raise AppError("This is not a Valhalla Photo Studio storyboard export file")
        if payload.get("version") != STORYBOARD_FORMAT_VERSION:
            raise AppError("Unsupported storyboard format version")
        db, _ = load_database()
        if payload.get("database") != database_fingerprint(db):
            raise AppError(
                "Storyboard belongs to a different database version. "
                "Restore its matching database.json before importing it."
            )
        config = payload.get("config")
        compact_shots = payload.get("shots")
        if not isinstance(config, dict) or not isinstance(compact_shots, list):
            raise AppError("Storyboard file is missing its configuration or shots")
        if "content_mode" not in config:
            raise AppError("Storyboard file is missing its content mode")
        if not compact_shots or len(compact_shots) > 10_000:
            raise AppError("Storyboard must contain between 1 and 10000 shots")
        args = parse_run_config(
            {key: value for key, value in config.items() if value is not None}, db
        )
        if args.prompt_seed is None:
            raise AppError("Storyboard file is missing its prompt seed")
        expected_total = args.count * (
            args.photoshoots if args.mode == "photoshoot" else 1
        )
        if len(compact_shots) != expected_total:
            raise AppError("Storyboard shot count does not match its configuration")
        rng = random.Random(args.prompt_seed)
        composer = Composer(db, rng, args.use_curated_defaults)
        index = {item["id"]: item for item in iter_content_items(db)}
        shots = []
        for position, compact in enumerate(compact_shots, 1):
            if not isinstance(compact, dict):
                raise AppError(f"Storyboard shot {position} is invalid")
            context = decode_database_refs(compact.get("context"), index)
            stage = decode_database_refs(compact.get("stage"), index)
            scene_delta = decode_database_refs(compact.get("scene"), index)
            if not all(isinstance(value, dict) for value in (context, stage, scene_delta)):
                raise AppError(f"Storyboard shot {position} is incomplete")
            if args.content_mode == "sfw" and not is_sfw_stage(stage):
                raise AppError(
                    f"Storyboard shot {position} uses stage {stage.get('id', 'unknown')}, "
                    "which is not allowed in SFW only mode"
                )
            scene = dict(context)
            scene.update(scene_delta)
            custom_values = compact.get("custom", {})
            if not isinstance(custom_values, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in custom_values.items()
            ):
                raise AppError(f"Storyboard shot {position} has invalid custom values")
            manual_fields = compact.get("manual_fields", [])
            if not isinstance(manual_fields, list) or not all(
                isinstance(field, str) for field in manual_fields
            ):
                raise AppError(f"Storyboard shot {position} has invalid manual fields")
            shot = {
                "number": _safe_int(compact.get("n"), "Shot number", 1, 10_000),
                "photoshoot_index": _safe_int(
                    compact.get("p"), "Photoshoot index", 0, 10_000
                ),
                "shot_index": _safe_int(
                    compact.get("s"), "Shot index", 0, 10_000
                ),
                "inference_seed": _safe_int(
                    compact.get("seed"), "Inference seed", 0, 2**64 - 1
                ),
                "context": context,
                "stage": stage,
                "stage_manual": bool(compact.get("stage_manual", False)),
                "manual_fields": sorted(set(manual_fields)),
                "scene": scene,
                "custom_values": custom_values,
            }
            self._apply_director_customs(shot, scene, context)
            if shot["number"] != position:
                raise AppError("Storyboard shot numbers must be consecutive")
            composer.validate_scene_rules(scene)
            serialize_shot(db, shot)
            shots.append(shot)
        storyboard_id = uuid.uuid4().hex
        record = {
            "id": storyboard_id,
            "created_at": str(payload.get("created_at") or _iso_now()),
            "db": db,
            "args": args,
            "composer": composer,
            "rng": rng,
            "shots": shots,
            "director_edited": bool(payload.get("director_edited", False)),
        }
        with self.lock:
            self.storyboards[storyboard_id] = record
            self.trim(self.storyboards, load_config()[0]["limits"]["max_storyboards"])
        return self.storyboard_payload(record)

    def reroll_shot(self, storyboard_id: str, number: int) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        shots = record["shots"]
        if not 1 <= number <= len(shots):
            raise AppError("Shot number is out of range")
        with self.lock:
            record["director_edited"] = True
            shot = shots[number - 1]
            shot["scene"] = record["composer"].resolve_scene(shot["context"], shot["stage"])
            self._apply_director_customs(shot, shot["scene"], shot["context"])
            if record["args"].inference_seed is None:
                shot["inference_seed"] = automatic_ui_seed()
            return serialize_shot(record["db"], shot)

    def randomize_shot_seed(self, storyboard_id: str, number: int) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        if not 1 <= number <= len(record["shots"]):
            raise AppError("Shot number is out of range")
        with self.lock:
            if any(
                job["storyboard_id"] == storyboard_id
                and job["status"] in {"queued", "running"}
                for job in self.jobs.values()
            ):
                raise AppError("Image variation cannot change while this storyboard is rendering")
            record["director_edited"] = True
            shot = record["shots"][number - 1]
            previous = shot["inference_seed"]
            while shot["inference_seed"] == previous:
                shot["inference_seed"] = automatic_ui_seed()
            shot["seed_manual"] = True
            return serialize_shot(record["db"], shot)

    def update_storyboard_seeds(
        self, storyboard_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        strategy = str(payload.get("inference_strategy", record["args"].inference_strategy))
        if strategy not in {"random", "fixed", "sequence"}:
            raise AppError("Inference seed strategy must be random, fixed, or sequence")
        base_seed = _optional_seed(
            payload.get("inference_seed"), "Image variation seed", 2**64 - 1
        )
        if strategy in {"fixed", "sequence"} and base_seed is None:
            base_seed = automatic_ui_seed()
        with self.lock:
            if any(
                job["storyboard_id"] == storyboard_id
                and job["status"] in {"queued", "running"}
                for job in self.jobs.values()
            ):
                raise AppError("Image variation cannot change while this storyboard is rendering")
            record["args"].inference_strategy = strategy
            record["args"].inference_seed = base_seed
            for shot in record["shots"]:
                if strategy == "random":
                    seed = automatic_ui_seed()
                elif strategy == "fixed":
                    seed = base_seed
                else:
                    material = (
                        f"{base_seed}:{shot['photoshoot_index']}:{shot['shot_index']}"
                    ).encode()
                    seed = deterministic_ui_seed(material)
                shot["inference_seed"] = seed
                shot["seed_manual"] = False
            return self.storyboard_payload(record)

    def create_job(self, storyboard_id: str, fast: bool, shot_numbers: list[int] | None = None) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        _, db_path = load_database()
        profile_mode = "preview" if fast else "production"
        source = workflow_source()
        live_workflow = live_mapping = None
        source_prompt_id = None
        if source == "live":
            workflow_profile, source_prompt_id, live_workflow, live_mapping = (
                snapshot_live_workflow(record["db"], bool(fast))
            )
        else:
            workflow_profile = load_workflow_profile_registry(record["db"], db_path).get(profile_mode)
            if not workflow_profile:
                raise AppError(f"No {profile_mode} workflow profile is selected")
        shot_numbers = shot_numbers or [shot["number"] for shot in record["shots"]]
        if not shot_numbers or any(number < 1 or number > len(record["shots"]) for number in shot_numbers):
            raise AppError("Render selection contains an invalid shot")
        selected_shots = copy.deepcopy([
            record["shots"][number - 1] for number in shot_numbers
        ])
        generation_mode = record["args"].mode
        render_tier = "preview" if fast else "production"
        render_groups: list[dict[str, Any]] = []
        groups_by_index: dict[int, dict[str, Any]] = {}
        for position, shot in enumerate(selected_shots, 1):
            group_index = (
                shot["photoshoot_index"] + 1 if generation_mode == "photoshoot" else 1
            )
            group = groups_by_index.get(group_index)
            if group is None:
                group = {
                    "group_index": group_index,
                    "positions": [],
                    "shot_numbers": [],
                }
                groups_by_index[group_index] = group
                render_groups.append(group)
            group["positions"].append(position)
            group["shot_numbers"].append(shot["number"])
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "storyboard_id": storyboard_id,
            "status": "queued",
            "fast": bool(fast),
            "workflow_profile": workflow_profile,
            "workflow_source": source,
            "source_prompt_id": source_prompt_id,
            "created_at": _iso_now(),
            "started_at": None,
            "finished_at": None,
            "completed": 0,
            "total": len(shot_numbers),
            "shot_numbers": shot_numbers,
            "kind": "shot" if len(shot_numbers) == 1 else "storyboard",
            "generation_mode": generation_mode,
            "render_tier": render_tier,
            "render_groups": render_groups,
            "current_shot": None,
            "progress": 0,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "outputs": [],
            "current_prompt": None,
            "logs": [{
                "time": _iso_now(), "type": "queued", "message": "Render job queued",
                "shot": None, "position": 0, "total": len(shot_numbers),
            }],
            "error": None,
            "cancel_requested": False,
            "_db": record["db"],
            "_mode": record["args"].mode,
            "_shots": selected_shots,
            "_workflow_template": live_workflow,
            "_workflow_mapping": live_mapping,
            "_frame_durations": [],
        }
        start_worker = False
        with self.lock:
            if any(preview["status"] in {"queued", "running"} for preview in self.previews.values()):
                raise AppError("Wait for the active shot preview to finish")
            max_jobs = load_config()[0]["limits"]["max_jobs"]
            while len(self.jobs) >= max_jobs:
                removable = next((
                    job_id for job_id, item in self.jobs.items()
                    if item["status"] not in {"queued", "running"}
                ), None)
                if removable is None:
                    raise AppError(f"Render queue is full ({max_jobs} jobs)")
                self.jobs.pop(removable)
            self.jobs[job_id] = job
            if not self._job_worker_running:
                self._job_worker_running = True
                start_worker = True
            payload = self.job_payload(job)
            payload["queue_position"] = sum(
                1 for item in self.jobs.values()
                if item["status"] == "queued"
            )
        if start_worker:
            threading.Thread(target=self._run_job_queue, daemon=True).start()
        return payload

    def create_video_job(
        self, source: str, relative_path: str, prompt: str, duration: int,
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db, db_path = load_database()
        source = str(source)
        if source not in {source_id for source_id, _ in proof_directories()}:
            raise AppError("Unknown proof source")
        prompt = str(prompt).strip()
        if not prompt:
            raise AppError("Video prompt cannot be empty")
        if len(prompt) > 6000:
            raise AppError("Video prompt is too long (maximum 6000 characters)")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 60:
            raise AppError("Video duration must be an integer from 1 to 60 seconds")
        source_path = proof_image_path(str(relative_path), str(source))
        if source_path.suffix.lower() not in IMAGE_SUFFIXES or not source_path.is_file():
            raise AppError("Video source must be an existing generated image")
        workflow_source_name = workflow_source("video")
        live_workflow = live_mapping = None
        source_prompt_id = None
        if workflow_source_name == "live":
            workflow_profile, source_prompt_id, live_workflow, live_mapping = snapshot_live_workflow(
                db, False, "video"
            )
        else:
            workflow_profile = load_workflow_profile_registry(db, db_path, "video").get("production")
            if not workflow_profile:
                raise AppError("No Video workflow profile is selected")
        seed = secrets.randbelow(2**63)
        job_id = uuid.uuid4().hex
        source_record = {
            "source": source,
            "relative_path": str(relative_path),
            "name": source_path.name,
        }
        if isinstance(source_metadata, dict):
            for key in (
                "key", "source_key", "source_image", "generation_mode", "render_tier",
                "group_index", "shot", "source_generation_mode", "source_render_tier",
                "source_group_index", "source_shot",
            ):
                value = source_metadata.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    source_record[key] = value
        source_record.setdefault("source_key", f"{source}:{relative_path}")
        job = {
            "id": job_id, "storyboard_id": None, "status": "queued", "fast": False,
            "workflow_profile": workflow_profile, "workflow_source": workflow_source_name,
            "source_prompt_id": source_prompt_id, "created_at": _iso_now(),
            "started_at": None, "finished_at": None, "completed": 0, "total": 1,
            "shot_numbers": [], "kind": "video", "generation_mode": "video",
            "render_tier": "production", "render_groups": [{
                "group_index": 1, "positions": [1], "shot_numbers": [1],
            }], "current_shot": None, "progress": 0, "elapsed_seconds": 0,
            "eta_seconds": None, "outputs": [], "current_prompt": None,
            "logs": [{"time": _iso_now(), "type": "queued", "message": "Video job queued", "shot": None, "position": 0, "total": 1}],
            "error": None, "cancel_requested": False, "_db": db, "_mode": "video",
            "_shots": [], "_workflow_template": live_workflow, "_workflow_mapping": live_mapping,
            "_frame_durations": [], "_media_type": "video", "_video_source": source_record,
            "_video_prompt": prompt, "_video_duration": duration, "_video_seed": seed,
        }
        start_worker = False
        with self.lock:
            if any(preview["status"] in {"queued", "running"} for preview in self.previews.values()):
                raise AppError("Wait for the active shot preview to finish")
            max_jobs = load_config()[0]["limits"]["max_jobs"]
            while len(self.jobs) >= max_jobs:
                removable = next((job_id for job_id, item in self.jobs.items() if item["status"] not in {"queued", "running"}), None)
                if removable is None:
                    raise AppError(f"Render queue is full ({max_jobs} jobs)")
                self.jobs.pop(removable)
            self.jobs[job_id] = job
            if not self._job_worker_running:
                self._job_worker_running = True
                start_worker = True
            payload = self.job_payload(job)
            payload["queue_position"] = sum(1 for item in self.jobs.values() if item["status"] == "queued")
        if start_worker:
            threading.Thread(target=self._run_job_queue, daemon=True).start()
        return payload

    def create_preview(
        self, storyboard_id: str, number: int, fast: bool
    ) -> dict[str, Any]:
        record = self.get_storyboard(storyboard_id)
        _, db_path = load_database()
        source = workflow_source()
        live_workflow = live_mapping = None
        source_prompt_id = None
        if source == "live":
            workflow_profile, source_prompt_id, live_workflow, live_mapping = (
                snapshot_live_workflow(record["db"], True)
            )
        else:
            workflow_profile = load_workflow_profile_registry(record["db"], db_path).get("preview")
            if not workflow_profile:
                raise AppError("No Preview workflow profile is selected")
        if not 1 <= number <= len(record["shots"]):
            raise AppError("Shot number is out of range")
        shot = record["shots"][number - 1]
        positive, negative, _ = compile_scene(record["db"], shot["scene"])
        debug_positive, _, _ = compile_scene(
            record["db"], shot["scene"], include_age=False
        )
        preview_id = uuid.uuid4().hex
        preview = {
            "id": preview_id,
            "storyboard_id": storyboard_id,
            "shot": number,
            "status": "queued",
            "created_at": _iso_now(),
            "finished_at": None,
            "started_at": None,
            "elapsed_seconds": 0,
            "prompt_id": None,
            "image_bytes": None,
            "mime_type": None,
            "error": None,
            "db": record["db"],
            "positive": positive,
            "negative": negative,
            "_debug_positive": debug_positive,
            "_scene": shot["scene"],
            "seed": shot["inference_seed"],
            "workflow_profile": workflow_profile,
            "workflow_source": source,
            "source_prompt_id": source_prompt_id,
            "_workflow_template": live_workflow,
            "_workflow_mapping": live_mapping,
        }
        with self.lock:
            if any(job["status"] in {"queued", "running"} for job in self.jobs.values()):
                raise AppError("Shot Preview is unavailable while a render job is active")
            if any(item["status"] in {"queued", "running"} for item in self.previews.values()):
                raise AppError("Another shot preview is already rendering")
            self.previews[preview_id] = preview
            self.trim(self.previews, load_config()[0]["limits"]["max_previews"])
        threading.Thread(
            target=self._run_preview, args=(preview_id,), daemon=True
        ).start()
        return self.preview_payload(preview)

    def preview_payload(self, preview: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": preview["id"],
            "storyboard_id": preview["storyboard_id"],
            "shot": preview["shot"],
            "status": preview["status"],
            "created_at": preview["created_at"],
            "finished_at": preview["finished_at"],
            "prompt_id": preview["prompt_id"],
            "image_url": (
                f"/api/previews/{preview['id']}/image"
                if preview["status"] == "completed" else None
            ),
            "error": preview["error"],
            "type": "preview",
            "positive": preview["positive"],
            "negative": preview["negative"],
            "seed": preview["seed"],
            "started_at": preview["started_at"],
            "elapsed_seconds": preview["elapsed_seconds"],
            "workflow_profile": preview["workflow_profile"],
            "workflow_source": preview["workflow_source"],
            "source_prompt_id": preview["source_prompt_id"],
        }
        if preview["status"] == "running" and preview.get("_started_monotonic") is not None:
            payload["elapsed_seconds"] = round(
                time.monotonic() - preview["_started_monotonic"], 1
            )
        return payload

    def get_preview(self, preview_id: str) -> dict[str, Any]:
        with self.lock:
            preview = self.previews.get(preview_id)
            if preview is None:
                raise AppError("Shot preview not found or expired")
            return self.preview_payload(preview)

    def preview_image(self, preview_id: str) -> tuple[bytes, str]:
        with self.lock:
            preview = self.previews.get(preview_id)
            if preview is None:
                raise AppError("Shot preview not found or expired")
            if preview["status"] != "completed" or preview["image_bytes"] is None:
                raise AppError("Shot preview image is not ready")
            return preview["image_bytes"], preview["mime_type"] or "image/png"

    def delete_preview(self, preview_id: str) -> dict[str, Any]:
        with self.lock:
            preview = self.previews.get(preview_id)
            if preview is None:
                return {"deleted": False}
            if preview["status"] in {"queued", "running"}:
                raise AppError("A rendering preview cannot be closed yet")
            self.previews.pop(preview_id, None)
        return {"deleted": True}

    def _run_preview(self, preview_id: str) -> None:
        with self.lock:
            preview = self.previews[preview_id]
            preview["status"] = "running"
            preview["started_at"] = _iso_now()
            preview["_started_monotonic"] = time.monotonic()
        try:
            db = preview["db"]
            _, db_path = load_database()
            workflow, mapping = preview.get("_workflow_template"), preview.get("_workflow_mapping")
            if workflow is None or mapping is None:
                workflow, mapping = load_workflow_runtime(db, db_path, True, preview["workflow_profile"])
            applied_lora_rules: list[dict[str, Any]] = []
            prompt_id, image_bytes, mime_type = generate_preview_image(
                db,
                preview["positive"],
                preview["negative"],
                preview["seed"],
                workflow,
                mapping,
                preview["_scene"],
                applied_lora_rules,
            )
            append_prompt_debug_record({
                "time": _iso_now(),
                "kind": "shot_preview",
                "result": f"preview:{preview_id}",
                "preview_id": preview_id,
                "storyboard_id": preview["storyboard_id"],
                "shot": preview["shot"],
                "seed": preview["seed"],
                "prompt_id": prompt_id,
                "workflow_profile": preview["workflow_profile"],
                "workflow_source": preview["workflow_source"],
                "positive": preview["_debug_positive"],
                "auxiliary_negative": preview["negative"],
                "lora_rules": applied_lora_rules,
            })
            with self.lock:
                preview["prompt_id"] = prompt_id
                preview["image_bytes"] = image_bytes
                preview["mime_type"] = mime_type
                preview["status"] = "completed"
        except Exception as exc:
            with self.lock:
                preview["status"] = "failed"
                preview["error"] = str(exc)
        finally:
            with self.lock:
                preview["finished_at"] = _iso_now()
                preview["elapsed_seconds"] = round(
                    time.monotonic() - preview["_started_monotonic"], 1
                )

    def job_frame_seconds(self, job: dict[str, Any]) -> float | None:
        durations = [
            float(value) for value in job.get("_frame_durations", [])[-7:]
            if isinstance(value, (int, float)) and value > 0
        ]
        if not durations and "fast" in job and job.get("workflow_profile"):
            durations = list(self._render_timings.get(
                (bool(job["fast"]), job["workflow_profile"]), []
            )[-7:])
        if not durations:
            return None
        ordered = sorted(durations)
        middle = len(ordered) // 2
        median = (
            ordered[middle] if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2
        )
        return round(median, 1)

    def pending_groups_payload(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        if (
            job["status"] not in {"queued", "running"}
            or not all(key in job for key in ("total", "completed", "shot_numbers"))
        ):
            return []
        estimate = self.job_frame_seconds(job)
        observed_at = _iso_now()
        active_eta = None
        if (
            estimate is not None and job["status"] == "running"
            and job.get("_shot_started_monotonic") is not None
        ):
            active_eta = max(
                0.0, estimate - (time.monotonic() - job["_shot_started_monotonic"])
            )
        groups = []
        for group in job["render_groups"]:
            remaining = [
                (position, shot)
                for position, shot in zip(group["positions"], group["shot_numbers"])
                if position > job["completed"]
            ]
            if not remaining:
                continue
            is_active = (
                job["status"] == "running"
                and remaining[0][0] == job["completed"] + 1
            )
            group_eta = None
            if is_active and estimate is not None and active_eta is not None:
                group_eta = active_eta + estimate * (len(remaining) - 1)
            groups.append({
                "group_key": (
                    f"job:{job['id']}:{job['generation_mode']}:"
                    f"{group['group_index']}:{job['render_tier']}"
                ),
                "job_id": job["id"],
                "generation_mode": job["generation_mode"],
                "render_tier": job["render_tier"],
                "group_index": group["group_index"],
                "positions": [item[0] for item in remaining],
                "shot_numbers": [item[1] for item in remaining],
                "status": "rendering" if is_active else "queued",
                "frame_eta_seconds": (
                    round(active_eta, 1) if is_active and active_eta is not None else None
                ),
                "group_eta_seconds": round(group_eta, 1) if group_eta is not None else None,
                "observed_at": observed_at,
            })
            if job["generation_mode"] == "video":
                source = job.get("_video_source") or {}
                groups[-1].update(
                    source_key=source.get("source_key")
                    or f"{source.get('source', 'output')}:{source.get('relative_path', '')}",
                    source_image=source.get("name"),
                )
        return groups

    def job_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in job.items() if not key.startswith("_")}
        estimate = self.job_frame_seconds(job)
        payload["observed_at"] = _iso_now()
        payload["estimated_frame_seconds"] = estimate
        payload["pending_groups"] = self.pending_groups_payload(job)
        if job["status"] == "running" and job.get("_started_monotonic") is not None:
            elapsed = time.monotonic() - job["_started_monotonic"]
            payload["elapsed_seconds"] = round(elapsed, 1)
        active_group = next(
            (group for group in payload["pending_groups"] if group["status"] == "rendering"),
            None,
        )
        estimate = payload["estimated_frame_seconds"]
        if active_group and estimate is not None:
            remaining = max(0, job["total"] - job["completed"] - 1)
            payload["eta_seconds"] = round(
                (active_group["frame_eta_seconds"] or 0) + estimate * remaining, 1
            )
        return payload

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise AppError("Render job not found or expired")
            payload = self.job_payload(job)
            pipeline = [
                item for item in self.jobs.values()
                if item["status"] in {"queued", "running"}
            ]
            position = next(
                (index for index, item in enumerate(pipeline) if item["id"] == job_id),
                None,
            )
            payload["queued_after"] = (
                max(0, len(pipeline) - position - 1) if position is not None else 0
            )
            return payload

    def jobs_payload(self) -> dict[str, Any]:
        with self.lock:
            jobs = [
                self.job_payload(job)
                for job in self.jobs.values()
            ]
            visible_previews = [
                preview for preview in self.previews.values()
                if not preview.get("logger_hidden", False)
            ]
            latest_preview = (
                self.preview_payload(visible_previews[-1])
                if visible_previews else None
            )
        active = next((job for job in jobs if job["status"] == "running"), None)
        if active is None:
            active = next((job for job in jobs if job["status"] == "queued"), None)
        queued = [job for job in jobs if job["status"] == "queued"]
        for position, job in enumerate(queued, 1):
            job["queue_position"] = position
            if active and active["id"] == job["id"]:
                active = job
        if active:
            active["queued_after"] = len(queued) - (1 if active["status"] == "queued" else 0)
        return {
            "active_job": active,
            "jobs": list(reversed(jobs)),
            "queued_jobs": queued,
            "latest_preview": latest_preview,
        }

    def clear_logger(self) -> dict[str, Any]:
        with self.lock:
            if any(job["status"] in {"queued", "running"} for job in self.jobs.values()):
                raise AppError("Logger cannot be cleared while a production render is active")
            if any(
                preview["status"] in {"queued", "running"}
                for preview in self.previews.values()
            ):
                raise AppError("Logger cannot be cleared while a preview render is active")
            cleared_jobs = len(self.jobs)
            self.jobs.clear()
            visible_previews = 0
            for preview in self.previews.values():
                if not preview.get("logger_hidden", False):
                    visible_previews += 1
                preview["logger_hidden"] = True
            return {
                "cleared": cleared_jobs + visible_previews,
                "jobs": cleared_jobs,
                "previews": visible_previews,
            }

    def has_active_render(self) -> bool:
        with self.lock:
            return any(job["status"] in {"queued", "running"} for job in self.jobs.values()) or any(
                preview["status"] in {"queued", "running"} for preview in self.previews.values()
            )

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise AppError("Render job not found or expired")
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["cancel_requested"] = True
                job["finished_at"] = _iso_now()
                job["logs"].append({
                    "time": _iso_now(), "type": "cancelled",
                    "message": "Queued render cancelled", "shot": None,
                    "position": 0, "total": job["total"],
                })
            elif job["status"] == "running":
                if job["completed"] >= job["total"]:
                    job["status"] = "completed"
                    job["progress"] = 100
                    job["eta_seconds"] = 0
                    job["finished_at"] = job["finished_at"] or _iso_now()
                elif not job["cancel_requested"]:
                    job["cancel_requested"] = True
                    job["logs"].append({
                        "time": _iso_now(), "type": "cancel_requested",
                        "message": "Cancellation requested", "shot": job.get("current_shot"),
                        "position": job["completed"], "total": job["total"],
                    })
            return self.job_payload(job)

    def _run_job_queue(self) -> None:
        while True:
            with self.lock:
                next_job = next(
                    (job for job in self.jobs.values() if job["status"] == "queued"),
                    None,
                )
                if next_job is None:
                    self._job_worker_running = False
                    return
                job_id = next_job["id"]
            self._run_job(job_id)

    def _run_video_job(
        self, job: dict[str, Any], job_id: str, db: dict[str, Any], db_path: Path,
        started: float,
    ) -> None:
        workflow, mapping = job.get("_workflow_template"), job.get("_workflow_mapping")
        if workflow is None or mapping is None:
            workflow, mapping = load_workflow_runtime(db, db_path, False, job["workflow_profile"], "video")
        source = job["_video_source"]
        source_path = proof_image_path(source["relative_path"], source["source"])
        prompt = job["_video_prompt"]
        seed = job["_video_seed"]
        with self.lock:
            if job["cancel_requested"]:
                job["status"] = "cancelled"
                return
            job["current_prompt"] = {
                "media_type": "video", "positive": prompt, "negative": "",
                "seed": seed, "source_image": source["name"],
            }
            job["logs"].append({
                "time": _iso_now(), "type": "shot_started", "message": "Rendering video",
                "shot": None, "position": 1, "total": 1, "positive": prompt,
                "negative": "", "seed": seed, "source_image": source["name"],
                "media_type": "video",
            })
        prompt_id, paths = generate_video_one(
            db, source_path, source, prompt, seed, job["_video_duration"],
            workflow, mapping, job["_run_id"],
        )
        for path in paths:
            append_prompt_debug_record({
                "time": _iso_now(), "kind": "video_render", "generation_mode": "video",
                "render_tier": "production", "result": path.name, "job_id": job_id,
                "source_image": source["name"], "source_key": f"{source['source']}:{source['relative_path']}",
                "prompt": prompt, "seed": seed, "duration": job["_video_duration"],
                "prompt_id": prompt_id, "workflow_profile": job["workflow_profile"],
                "workflow_source": job["workflow_source"],
            })
        with self.lock:
            elapsed = time.monotonic() - started
            job["completed"] = 1
            job["progress"] = 100
            job["elapsed_seconds"] = round(elapsed, 1)
            job["eta_seconds"] = 0
            shot_outputs = []
            for path in paths:
                published = output_payload(path)
                source_key = source.get("source_key") or published.get("source_key")
                source_image = source.get("name") or source.get("source_image") or published.get("source_image")
                published.update(
                    prompt_id=prompt_id, media_type="video", generation_mode="video",
                    render_tier="production", group_index=1,
                    source_image=source_image,
                    source_key=source_key or f"{source['source']}:{source['relative_path']}",
                    video_prompt=prompt, video_duration=job["_video_duration"], video_seed=seed,
                    group_key=f"job:{job_id}:video:1:production",
                )
                job["outputs"].append(published)
                shot_outputs.append(published)
            job["logs"][-1].update({
                "type": "shot_completed", "position": 1, "elapsed_seconds": round(elapsed, 1),
                "duration_seconds": round(time.monotonic() - started, 1),
                "video_url": shot_outputs[0]["url"] if shot_outputs else None,
            })
            job["current_prompt"]["video_url"] = shot_outputs[0]["url"] if shot_outputs else None
            job["status"] = "completed"

    def _run_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] != "queued":
                return
            job["status"] = "running"
            job["started_at"] = _iso_now()
            job["_started_monotonic"] = time.monotonic()
            job["logs"].append({
                "time": _iso_now(), "type": "started", "message": "Workflow started",
                "shot": None, "position": 0, "total": job["total"],
            })
        started = time.monotonic()
        try:
            db = job["_db"]
            _, db_path = load_database()
            workflow, mapping = job.get("_workflow_template"), job.get("_workflow_mapping")
            if workflow is None or mapping is None:
                workflow, mapping = load_workflow_runtime(
                    db, db_path, job["fast"], job["workflow_profile"],
                    job.get("_media_type", "image"),
                )
                job["_workflow_template"], job["_workflow_mapping"] = workflow, mapping
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            with self.lock:
                job["_run_id"] = run_id
            if job.get("_media_type") == "video":
                self._run_video_job(job, job_id, db, db_path, started)
                return
            selected_shots = job["_shots"]
            for completed_index, shot in enumerate(selected_shots, 1):
                shot_started = time.monotonic()
                with self.lock:
                    if job["cancel_requested"]:
                        job["status"] = "cancelled"
                        job["logs"].append({
                            "time": _iso_now(), "type": "cancelled", "message": "Render job cancelled",
                            "shot": None, "position": job["completed"], "total": job["total"],
                        })
                        break
                    job["current_shot"] = shot["number"]
                    job["_shot_started_monotonic"] = shot_started
                positive, negative, _ = compile_scene(db, shot["scene"])
                debug_positive, _, _ = compile_scene(
                    db, shot["scene"], include_age=False
                )
                with self.lock:
                    job["current_prompt"] = {
                        "shot": shot["number"], "position": completed_index,
                        "positive": positive, "negative": negative,
                        "seed": shot["inference_seed"],
                    }
                    shot_log = {
                        "time": _iso_now(), "type": "shot_started",
                        "message": f"Rendering shot {shot['number']}",
                        "shot": shot["number"], "position": completed_index,
                        "total": len(selected_shots), "seed": shot["inference_seed"],
                        "positive": positive, "negative": negative,
                    }
                    job["logs"].append(shot_log)
                applied_lora_rules: list[dict[str, Any]] = []
                prompt_id, paths = generate_one(
                    db, db_path, positive, negative, shot["inference_seed"],
                    job["_mode"], shot["shot_index"], shot["photoshoot_index"],
                    run_id, job["render_tier"], job["fast"], workflow, mapping,
                    shot["scene"], applied_lora_rules,
                )
                for path in paths:
                    group_index = (
                        shot["photoshoot_index"] + 1
                        if job["generation_mode"] == "photoshoot" else 1
                    )
                    append_prompt_debug_record({
                        "time": _iso_now(),
                        "kind": "render",
                        "generation_mode": job["generation_mode"],
                        "render_tier": job["render_tier"],
                        "group_index": group_index,
                        "result": path.name,
                        "job_id": job_id,
                        "storyboard_id": job["storyboard_id"],
                        "shot": shot["number"],
                        "seed": shot["inference_seed"],
                        "prompt_id": prompt_id,
                        "workflow_profile": job["workflow_profile"],
                        "workflow_source": job["workflow_source"],
                        "positive": debug_positive,
                        "auxiliary_negative": negative,
                        "lora_rules": applied_lora_rules,
                    })
                elapsed = time.monotonic() - started
                completed = completed_index
                remaining = len(selected_shots) - completed
                with self.lock:
                    frame_duration = round(time.monotonic() - shot_started, 1)
                    job["_frame_durations"].append(frame_duration)
                    timing_key = (bool(job["fast"]), job["workflow_profile"])
                    self._render_timings.setdefault(timing_key, []).append(frame_duration)
                    self._render_timings[timing_key] = self._render_timings[timing_key][-20:]
                    job["completed"] = completed
                    job["progress"] = round(completed * 100 / len(selected_shots), 1)
                    job["elapsed_seconds"] = round(elapsed, 1)
                    job["eta_seconds"] = round(elapsed / completed * remaining, 1) if remaining else 0
                    shot_outputs = []
                    for path in paths:
                        published = output_payload(path)
                        group_index = (
                            shot["photoshoot_index"] + 1
                            if job["generation_mode"] == "photoshoot" else 1
                        )
                        published.update(
                            prompt_id=prompt_id,
                            shot=shot["number"],
                            group_key=(
                                f"job:{job_id}:{job['generation_mode']}:"
                                f"{group_index}:{job['render_tier']}"
                            ),
                            generation_mode=job["generation_mode"],
                            render_tier=job["render_tier"],
                            group_index=group_index,
                        )
                        job["outputs"].append(published)
                        shot_outputs.append(published)
                    shot_log.update({
                        "type": "shot_completed",
                        "position": completed,
                        "elapsed_seconds": round(elapsed, 1),
                        "duration_seconds": frame_duration,
                        "image_url": shot_outputs[0]["url"] if shot_outputs else None,
                    })
                    if job.get("current_prompt") is not None:
                        job["current_prompt"]["image_url"] = shot_log["image_url"]
                    if completed == len(selected_shots):
                        job["status"] = "completed"
                        job["progress"] = 100
                        job["eta_seconds"] = 0
            with self.lock:
                if job["status"] == "running":
                    job["status"] = "completed"
        except Exception as exc:
            with self.lock:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["logs"].append({
                    "time": _iso_now(), "type": "error", "message": str(exc),
                    "shot": job.get("current_shot"), "position": job["completed"], "total": job["total"],
                })
        finally:
            with self.lock:
                job["elapsed_seconds"] = round(time.monotonic() - started, 1)
                job["finished_at"] = _iso_now()
                job["current_shot"] = None
                job.pop("_shot_started_monotonic", None)


WEB_STATE = WebState()


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
IMAGE_MEDIA_REF_RE = re.compile(r"(?:^|_)(?P<media_id>-?\d+)_image_(?P<image_number>\d+)$", re.IGNORECASE)
VIDEO_SOURCE_RE = re.compile(
    r"_video_from_(?P<source_token>.+)_(?P<video_seed>-?\d+)_video_\d+\.[^.]+$",
    re.IGNORECASE,
)
SOURCE_TOKEN_DIGEST_RE = re.compile(r"^(?P<base>.+)~(?P<digest>[0-9a-f]{16})$", re.IGNORECASE)
GALLERY_BENCHMARK_COUNT = 0
GALLERY_BENCHMARK_SOURCES = 10


def image_media_ref(name: str) -> dict[str, str] | None:
    """Extract the source media ID and output number from a generated image name."""
    match = IMAGE_MEDIA_REF_RE.search(Path(name).stem)
    if not match:
        return None
    return {
        "media_id": match.group("media_id"),
        "ref": f"{match.group('media_id')}_image_{match.group('image_number')}",
    }


def source_identity_digest(source: str, relative_path: str) -> str:
    """Return a stable short identity for one configured proof file."""
    value = f"{source}:{Path(relative_path).as_posix()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def source_media_token(source: str, relative_path: str, name: str) -> str:
    """Encode a visible media reference plus its collision-resistant source identity."""
    ref = image_media_ref(name)
    base = ref["ref"] if ref else re.sub(
        r"[^A-Za-z0-9._-]+", "-", Path(name).stem
    ).strip("-._")[:120] or "source"
    return f"{base}~{source_identity_digest(source, relative_path)}"


def video_source_token(name: str) -> str | None:
    match = VIDEO_SOURCE_RE.search(name)
    return match.group("source_token") if match else None


def output_directory() -> Path:
    config, path = load_config()
    return resolve_path(path.parent, config["storage"]["output_dir"])


def output_metadata_path(path: Path) -> Path:
    """Return the legacy sidecar path so old files are cleaned up on deletion."""
    return path.with_name(f".{path.name}.meta.json")


def proof_directories() -> list[tuple[str, Path]]:
    config, path = load_config()
    configured = config["storage"]["proofs_dir"]
    values = [configured] if isinstance(configured, str) else configured
    output = output_directory().resolve()
    candidates = [
        (f"proof-{index}", resolve_path(path.parent, value))
        for index, value in enumerate(values, 1)
    ] + [("output", output)]
    # Always retain output_dir under the stable "output" source, even if it was
    # repeated in proofs_dir. Additional proof sources load before live output.
    seen: set[Path] = {output}
    result = []
    for source, directory in candidates:
        resolved = directory.resolve()
        if source == "output" or resolved not in seen:
            seen.add(resolved)
            result.append((source, resolved))
    return result


def proof_directory(source: str) -> Path:
    if source == "output":
        return output_directory()
    directory = next((path for source_id, path in proof_directories() if source_id == source), None)
    if directory is None:
        raise AppError("Unknown proof source")
    return directory


def proof_image_path(relative_path: str, source: str = "output") -> Path:
    if not relative_path:
        raise AppError("Invalid proof path")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise AppError("Invalid proof path")
    root = proof_directory(source).resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise AppError("Invalid proof path")
    return target


def is_live_output_path(path: Path, output: Path | None = None) -> bool:
    output = (output or output_directory()).resolve()
    resolved = path.resolve()
    return resolved == output or output in resolved.parents


def proof_source_files(source: str, directory: Path, live_output: Path) -> Iterable[Path]:
    if source == "output":
        yield from directory.iterdir()
        return
    proof_root = directory.resolve()
    for current, directories, filenames in os.walk(proof_root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if proof_root in (current_path / name).resolve().parents
            and not is_live_output_path(current_path / name, live_output)
        ]
        yield from (current_path / name for name in filenames)


def output_payload(path: Path, source: str = "output", root: Path | None = None) -> dict[str, Any]:
    root = (root or path.parent).resolve()
    try:
        relative_path = path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise AppError("Proof image is outside its configured directory") from exc
    match = re.search(r"_shot_(\d+)_", path.name)
    stat = path.stat()
    encoded_path = quote(relative_path, safe="")
    is_video = path.suffix.lower() in VIDEO_SUFFIXES
    source_image = None
    source_media_id = None
    source_media_ref = None
    source_key = None
    if is_video:
        source_match = VIDEO_SOURCE_RE.search(path.name)
        source_image = source_match.group("source_token") if source_match else None
        parsed_source = image_media_ref((source_image or "").split("~", 1)[0])
        if parsed_source:
            source_media_id = parsed_source["media_id"]
            source_media_ref = parsed_source["ref"]
            # This is a provisional key for direct job polling. The filesystem
            # listing replaces it with the actual image key after resolution.
            source_key = f"{source}:{source_media_ref}"
    payload = {
        "name": path.name,
        "relative_path": relative_path,
        "source": source,
        "key": f"{source}:{relative_path}",
        "url": f"/api/outputs/{encoded_path}?source={source}",
        "thumbnail_url": f"/api/thumbnails/{encoded_path}?source={source}&v={stat.st_mtime_ns}",
        "shot": int(match.group(1)) if match else None,
        "size": stat.st_size,
    }
    if is_video:
        payload.update({
            "media_type": "video",
            "source_image": source_image,
            "source_media_id": source_media_id,
            "source_media_ref": source_media_ref,
            "source_key": source_key,
            "source_relative_path": None,
            "source_generation_mode": None,
            "source_render_tier": None,
            "source_group_index": None,
            "source_shot": None,
            "video_prompt": None,
            "video_duration": None,
        })
    else:
        payload["media_type"] = "image"
    return payload


def _thumbnail_cache_remove(name: str | None = None, source: str | None = None) -> None:
    global THUMBNAIL_CACHE_BYTES
    with THUMBNAIL_CACHE_LOCK:
        keys = list(THUMBNAIL_CACHE) if name is None else [
            key for key in THUMBNAIL_CACHE if key[1] == name and (source is None or key[0] == source)
        ]
        for key in keys:
            THUMBNAIL_CACHE_BYTES -= len(THUMBNAIL_CACHE.pop(key))


def thumbnail_cache_max_bytes() -> int:
    config, _ = load_config()
    return config["gallery"]["thumbnail_cache_mb"] * 1024 * 1024


def _generate_thumbnail(target: Path) -> bytes:
    if target.suffix.lower() in VIDEO_SUFFIXES:
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(target), "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                check=True, capture_output=True, timeout=30,
            )
            if not result.stdout:
                raise OSError("ffmpeg returned an empty poster frame")
            return _generate_thumbnail_bytes(result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError(f"Could not create video thumbnail: {exc}") from exc
    if Image is None or ImageOps is None:
        raise AppError("Thumbnail support requires Pillow; restart with launcher.sh to install it")
    try:
        return _generate_thumbnail_bytes(target)
    except (OSError, ValueError) as exc:
        raise AppError(f"Could not create thumbnail: {exc}") from exc


def _generate_thumbnail_bytes(source_bytes: bytes | Path) -> bytes:
    if Image is None or ImageOps is None:
        raise AppError("Thumbnail support requires Pillow; restart with launcher.sh to install it")
    try:
        source_file = Image.open(BytesIO(source_bytes)) if isinstance(source_bytes, bytes) else Image.open(source_bytes)
        with source_file as source:
            source.seek(0)
            thumbnail = ImageOps.exif_transpose(source)
            thumbnail_max_edge = load_config()[0]["gallery"]["thumbnail_max_edge"]
            thumbnail.thumbnail((thumbnail_max_edge, thumbnail_max_edge), Image.Resampling.LANCZOS)
            if thumbnail.mode not in {"RGB", "L"}:
                if "A" in thumbnail.getbands():
                    background = Image.new("RGB", thumbnail.size, "#111318")
                    background.paste(thumbnail, mask=thumbnail.getchannel("A"))
                    thumbnail = background
                else:
                    thumbnail = thumbnail.convert("RGB")
            buffer = BytesIO()
            thumbnail.save(buffer, format="JPEG", quality=82, optimize=True)
            body = buffer.getvalue()
    except (OSError, ValueError) as exc:
        raise AppError(f"Could not create thumbnail: {exc}") from exc
    return body


def output_thumbnail(relative_path: str, source: str = "output") -> bytes:
    global THUMBNAIL_CACHE_BYTES
    target = proof_image_path(relative_path, source)
    if target.suffix.lower() not in MEDIA_SUFFIXES:
        raise AppError("Only generated media files can be viewed")
    if not target.is_file():
        raise AppError("Output not found")
    stat = target.stat()
    key = (source, relative_path, stat.st_mtime_ns, stat.st_size)
    with THUMBNAIL_CACHE_LOCK:
        cached = THUMBNAIL_CACHE.get(key)
        if cached is not None:
            THUMBNAIL_CACHE.move_to_end(key)
            return cached
        future = THUMBNAIL_IN_FLIGHT.get(key)
        owns_generation = future is None
        if future is None:
            future = Future()
            THUMBNAIL_IN_FLIGHT[key] = future
    if not owns_generation:
        return future.result()
    try:
        body = _generate_thumbnail(target)
        with THUMBNAIL_CACHE_LOCK:
            previous = THUMBNAIL_CACHE.pop(key, None)
            if previous is not None:
                THUMBNAIL_CACHE_BYTES -= len(previous)
            THUMBNAIL_CACHE[key] = body
            THUMBNAIL_CACHE_BYTES += len(body)
            cache_limit = thumbnail_cache_max_bytes()
            while THUMBNAIL_CACHE_BYTES > cache_limit and THUMBNAIL_CACHE:
                _, evicted = THUMBNAIL_CACHE.popitem(last=False)
                THUMBNAIL_CACHE_BYTES -= len(evicted)
        future.set_result(body)
        return body
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with THUMBNAIL_CACHE_LOCK:
            THUMBNAIL_IN_FLIGHT.pop(key, None)


def list_output_images() -> list[dict[str, Any]]:
    paths = []
    live_output = output_directory().resolve()
    for source, directory in proof_directories():
        if not directory.is_dir():
            continue
        source_paths = [
            path for path in proof_source_files(source, directory, live_output)
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
            and directory.resolve() in path.resolve().parents
        ]
        source_paths.sort(key=lambda path: (path.name, path.relative_to(directory).as_posix()))
        paths.extend((source, directory, path) for path in source_paths)
    if GALLERY_BENCHMARK_COUNT:
        if not paths:
            raise AppError("Gallery benchmark requires at least one existing output image")
        sources = paths[-GALLERY_BENCHMARK_SOURCES:]
        outputs = []
        for index in range(GALLERY_BENCHMARK_COUNT):
            source_id, root, source = sources[index % len(sources)]
            payload = output_payload(source, source_id, root)
            payload["name"] = f"benchmark_{index + 1:05d}_{source.name}"
            payload["key"] = f"benchmark:{index + 1}"
            separator = "&" if "?" in payload["thumbnail_url"] else "?"
            payload["thumbnail_url"] += f"{separator}benchmark={index + 1}"
            payload["shot"] = index + 1
            outputs.append(payload)
        return outputs
    outputs = [output_payload(path, source, root) for source, root, path in paths]
    images = [item for item in outputs if item["media_type"] == "image"]
    by_token: dict[str, list[dict[str, Any]]] = {}

    def add_index_token(token: str | None, item: dict[str, Any]) -> None:
        if not token:
            return
        bucket = by_token.setdefault(token, [])
        if item not in bucket:
            bucket.append(item)

    for item in images:
        ref = image_media_ref(item["name"])
        add_index_token(item["key"], item)
        add_index_token(
            source_identity_digest(item["source"], item["relative_path"]), item
        )
        add_index_token(Path(item["name"]).stem, item)
        add_index_token(
            re.sub(r"[^A-Za-z0-9._-]+", "-", Path(item["name"]).stem)
            .strip("-._")[:120] or "source",
            item,
        )
        if ref:
            add_index_token(ref["ref"], item)
            add_index_token(ref["media_id"], item)
            add_index_token(
                source_media_token(item["source"], item["relative_path"], item["name"]),
                item,
            )

    for item in outputs:
        if item["media_type"] != "video":
            continue
        token = video_source_token(item["name"])
        if not token:
            continue
        candidates = by_token.get(token, [])
        if len(candidates) != 1:
            digest_match = SOURCE_TOKEN_DIGEST_RE.fullmatch(token)
            if digest_match:
                candidates = by_token.get(digest_match.group("digest"), [])
            elif token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
                candidates = by_token.get(token, [])
        if len(candidates) == 1:
            original = candidates[0]
            item.update(
                source_key=original["key"],
                source_image=original["name"],
                source_relative_path=original["relative_path"],
                source_generation_mode=original.get("generation_mode"),
                source_render_tier=original.get("render_tier"),
                source_group_index=original.get("group_index"),
                source_shot=original["shot"],
            )
        else:
            # Keep an orphan video independent. A seed-only legacy token may
            # refer to several images, so never bind it to an arbitrary one.
            item["source_key"] = None
    return outputs


def ensure_outputs_idle() -> None:
    with WEB_STATE.lock:
        active = any(job["status"] in {"queued", "running"} for job in WEB_STATE.jobs.values())
    if active:
        raise AppError("Outputs cannot be deleted while a render job is active")


def delete_output_image(relative_path: str, source: str = "output") -> dict[str, Any]:
    if GALLERY_BENCHMARK_COUNT:
        raise AppError("Output deletion is disabled in gallery benchmark mode")
    target = proof_image_path(relative_path, source)
    if target.suffix.lower() not in MEDIA_SUFFIXES:
        raise AppError("Only generated media files can be deleted")
    if not target.is_file():
        raise AppError("Output not found")
    with WEB_STATE.lock:
        # A completed frame is safe to remove while the next frame renders. Guard
        # only a file from the active run that has not yet been published as an
        # output, because it may still be in the middle of being written.
        for job in WEB_STATE.jobs.values():
            if source != "output" or job["status"] not in {"queued", "running"}:
                continue
            run_id = job.get("_run_id")
            published = {
                item.get("relative_path", item["name"]) for item in job.get("outputs", [])
            }
            if (
                run_id and relative_path.startswith(f"{run_id}_")
                and relative_path not in published
            ):
                raise AppError("This frame is still being written and cannot be deleted yet")
        try:
            target.unlink()
        except OSError as exc:
            raise AppError(f"Could not delete output: {exc}") from exc
        try:
            output_metadata_path(target).unlink(missing_ok=True)
        except OSError as exc:
            raise AppError(f"Could not delete media metadata: {exc}") from exc
        _thumbnail_cache_remove(relative_path, source)
        # Prevent subsequent job polling from restoring a deleted card in the UI.
        for job in WEB_STATE.jobs.values():
            if source != "output":
                continue
            job["outputs"] = [
                item for item in job.get("outputs", [])
                if item.get("relative_path", item["name"]) != relative_path
            ]
    return {"ok": True, "deleted": relative_path, "source": source}


def delete_all_output_images() -> dict[str, Any]:
    if GALLERY_BENCHMARK_COUNT:
        raise AppError("Output deletion is disabled in gallery benchmark mode")
    ensure_outputs_idle()
    live_output = output_directory().resolve()
    targets = [
        (source, directory, path)
        for source, directory in proof_directories() if directory.is_dir()
        for path in proof_source_files(source, directory, live_output)
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
        and directory.resolve() in path.resolve().parents
    ]
    deleted: list[dict[str, str]] = []
    for source, directory, target in targets:
        relative_path = target.relative_to(directory).as_posix()
        try:
            target.unlink()
            deleted.append({"name": target.name, "relative_path": relative_path, "source": source})
            output_metadata_path(target).unlink(missing_ok=True)
        except OSError as exc:
            raise AppError(
                f"Deleted {len(deleted)} media files, then could not delete {target.name}: {exc}"
            ) from exc
    _thumbnail_cache_remove()
    return {"ok": True, "deleted": len(deleted), "files": deleted}


def application_status(check_comfy: bool = True) -> dict[str, Any]:
    db, db_path = load_database()
    settings = db["settings"]
    config, config_file = load_config()
    workflow_profiles = list_workflow_profiles(db, db_path, "image")
    video_profiles = list_workflow_profiles(db, db_path, "video")
    live_workflow = workflow_profiles["source"] == "live"
    live_video_workflow = video_profiles["source"] == "live"
    image_ready = live_workflow or bool(
        workflow_profiles["production"] and workflow_profiles["preview"]
    )
    video_ready = live_video_workflow or bool(video_profiles["production"])
    output_path = resolve_path(config_file.parent, config["storage"]["output_dir"])
    selectable = sum(1 for item in iter_content_items(db) if not item.get("disabled", False))
    comfy_config = config["comfy"]
    comfy = {
        "url": comfy_config["url"],
        "online": False,
        "message": "Not checked",
        "refresh_seconds": comfy_config["status_refresh_seconds"],
    }
    if check_comfy:
        try:
            session, url, _ = comfy_session(db)
            response = session.get(
                f"{url}/system_stats", timeout=float(comfy_config["status_timeout_seconds"])
            )
            response.raise_for_status()
            comfy.update(online=True, message="Connected")
        except Exception as exc:
            comfy["message"] = str(exc)
    progression = settings.get("photoshoot_progression", {})
    return {
        "app": "Valhalla Photo Studio",
        "version": APP_VERSION,
        "comfy": comfy,
        "workflow": {
            "ready": image_ready,
            "name": "Live ComfyUI" if live_workflow else f"{len(workflow_profiles['profiles'])} profiles",
            **workflow_profiles,
            "image": {**workflow_profiles, "ready": image_ready},
            "video": {
                **video_profiles,
                "ready": video_ready,
                "name": "Live ComfyUI" if live_video_workflow else f"{len(video_profiles['profiles'])} profiles",
            },
        },
        "output": {"path": str(output_path), "exists": output_path.is_dir()},
        "catalog_records": selectable,
        "defaults": {
            "nsfw_percent": progression.get("nsfw_final_percent", 50),
            "plateau_percent": progression.get("explicit_plateau_percent", 30),
        },
        "interface": {
            "privacy": {
                "auto_cover_minutes": config["interface"]["privacy"]["auto_cover_minutes"],
            },
        },
    }


class ValhallaHandler(BaseHTTPRequestHandler):
    server_version = f"Valhalla/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AppError("Invalid Content-Length") from exc
        if length > 32_000_000:
            raise AppError("Request body is too large (maximum 32 MB)")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise AppError("Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise AppError("Request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/status":
                self.send_json(application_status())
            elif path == "/api/workflow/profiles":
                media_type = parse_qs(urlparse(self.path).query).get("media", ["image"])[0]
                db, db_path = load_database()
                self.send_json(list_workflow_profiles(db, db_path, media_type))
            elif path == "/api/workflow/capture-candidate":
                media_type = parse_qs(urlparse(self.path).query).get("media", ["image"])[0]
                db, _ = load_database()
                self.send_json(workflow_capture_candidate(db, media_type))
            elif path.endswith("/export") and path.startswith("/api/storyboards/"):
                storyboard_id = path.split("/")[3]
                self.send_json(WEB_STATE.export_storyboard(storyboard_id))
            elif path.endswith("/director") and path.startswith("/api/storyboards/"):
                storyboard_id = path.split("/")[3]
                query = parse_qs(urlparse(self.path).query)
                shot = _safe_int(query.get("shot", ["1"])[0], "Shot", 1, 10_000)
                self.send_json(WEB_STATE.director_payload(storyboard_id, shot))
            elif path.startswith("/api/storyboards/"):
                storyboard_id = path.split("/")[3]
                self.send_json(WEB_STATE.storyboard_payload(WEB_STATE.get_storyboard(storyboard_id)))
            elif path == "/api/jobs":
                self.send_json(WEB_STATE.jobs_payload())
            elif path.startswith("/api/jobs/"):
                self.send_json(WEB_STATE.get_job(path.split("/")[3]))
            elif path.endswith("/image") and path.startswith("/api/previews/"):
                self.serve_preview_image(path.split("/")[3])
            elif path.startswith("/api/previews/"):
                self.send_json(WEB_STATE.get_preview(path.split("/")[3]))
            elif path == "/api/outputs":
                self.send_json({
                    "outputs": list_output_images(),
                    "benchmark": bool(GALLERY_BENCHMARK_COUNT),
                })
            elif path.startswith("/api/thumbnails/"):
                source = parse_qs(urlparse(self.path).query).get("source", ["output"])[0]
                self.serve_thumbnail(unquote(path.removeprefix("/api/thumbnails/")), source)
            elif path.startswith("/api/outputs/"):
                source = parse_qs(urlparse(self.path).query).get("source", ["output"])[0]
                self.serve_output(unquote(path.removeprefix("/api/outputs/")), source)
            elif path.startswith("/api"):
                self.send_json({"error": "API endpoint not found"}, HTTPStatus.NOT_FOUND)
            else:
                self.serve_static(path)
        except AppError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.send_json({"error": f"Internal server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/storyboards":
                self.send_json(WEB_STATE.create_storyboard(self.read_json()), HTTPStatus.CREATED)
            elif path == "/api/storyboards/import":
                self.send_json(WEB_STATE.import_storyboard(self.read_json()), HTTPStatus.CREATED)
            elif path.endswith("/director") and path.startswith("/api/storyboards/"):
                storyboard_id = path.split("/")[3]
                self.send_json(WEB_STATE.update_director(storyboard_id, self.read_json()))
            elif path.endswith("/seeds") and path.startswith("/api/storyboards/"):
                storyboard_id = path.split("/")[3]
                self.send_json(WEB_STATE.update_storyboard_seeds(storyboard_id, self.read_json()))
            elif path.endswith("/reroll") and path.startswith("/api/storyboards/"):
                parts = path.split("/")
                self.send_json(WEB_STATE.reroll_shot(parts[3], _safe_int(parts[5], "Shot", 1, 10000)))
            elif path.endswith("/seed") and path.startswith("/api/storyboards/"):
                parts = path.split("/")
                self.send_json(WEB_STATE.randomize_shot_seed(parts[3], _safe_int(parts[5], "Shot", 1, 10000)))
            elif path.endswith("/render") and path.startswith("/api/storyboards/"):
                parts = path.split("/")
                payload = self.read_json()
                number = _safe_int(parts[5], "Shot", 1, 10_000)
                self.send_json(
                    WEB_STATE.create_job(parts[3], bool(payload.get("fast", False)), [number]),
                    HTTPStatus.ACCEPTED,
                )
            elif path == "/api/jobs":
                payload = self.read_json()
                self.send_json(
                    WEB_STATE.create_job(str(payload.get("storyboard_id", "")), bool(payload.get("fast", False))),
                    HTTPStatus.ACCEPTED,
                )
            elif path == "/api/previews":
                payload = self.read_json()
                self.send_json(
                    WEB_STATE.create_preview(
                        str(payload.get("storyboard_id", "")),
                        _safe_int(payload.get("shot"), "Shot", 1, 10_000),
                        bool(payload.get("fast", False)),
                    ),
                    HTTPStatus.ACCEPTED,
                )
            elif path.endswith("/cancel") and path.startswith("/api/jobs/"):
                self.send_json(WEB_STATE.cancel_job(path.split("/")[3]))
            elif path == "/api/videos":
                payload = self.read_json()
                self.send_json(WEB_STATE.create_video_job(
                    str(payload.get("source", "output")),
                    str(payload.get("relative_path", "")),
                    str(payload.get("prompt", "")),
                    _safe_int(payload.get("duration"), "Video duration", 1, 60),
                    payload.get("source_metadata") if isinstance(payload.get("source_metadata"), dict) else None,
                ), HTTPStatus.ACCEPTED)
            elif path == "/api/workflow/capture":
                if WEB_STATE.has_active_render():
                    raise AppError("Workflow profiles cannot be captured while rendering is active")
                payload = self.read_json()
                media_type = str(payload.get("media", "image"))
                db, db_path = load_database()
                profile = capture_workflow_profile(
                    db, db_path, str(payload.get("name", "")), bool(payload.get("replace", False)), media_type
                )
                self.send_json({"ok": True, "message": "Workflow profile captured", "profile": profile})
            elif path == "/api/workflow/profiles/select":
                payload = self.read_json()
                db, db_path = load_database()
                self.send_json(select_workflow_profiles(
                    db, db_path, str(payload.get("production", "")),
                    str(payload.get("preview", "")), str(payload.get("source", "profiles")),
                    str(payload.get("media", "image")),
                ))
            elif path.endswith("/rename") and path.startswith("/api/workflow/profiles/"):
                if WEB_STATE.has_active_render():
                    raise AppError("Workflow profiles cannot be renamed while the render queue is active")
                payload = self.read_json()
                db, db_path = load_database()
                media_type = parse_qs(urlparse(self.path).query).get("media", ["image"])[0]
                self.send_json(rename_workflow_profile(
                    db, db_path, path.split("/")[4], str(payload.get("name", "")), media_type
                ))
            else:
                self.send_json({"error": "API endpoint not found"}, HTTPStatus.NOT_FOUND)
        except AppError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"Internal server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/outputs":
                self.send_json(delete_all_output_images())
            elif path == "/api/logger":
                self.send_json(WEB_STATE.clear_logger())
            elif path.startswith("/api/outputs/"):
                name = unquote(path.removeprefix("/api/outputs/"))
                source = parse_qs(urlparse(self.path).query).get("source", ["output"])[0]
                self.send_json(delete_output_image(name, source))
            elif path.startswith("/api/previews/"):
                self.send_json(WEB_STATE.delete_preview(path.split("/")[3]))
            elif path.startswith("/api/workflow/profiles/"):
                if WEB_STATE.has_active_render():
                    raise AppError("Workflow profiles cannot be deleted while the render queue is active")
                media_type = parse_qs(urlparse(self.path).query).get("media", ["image"])[0]
                db, db_path = load_database()
                self.send_json(delete_workflow_profile(db, db_path, path.split("/")[4], media_type))
            else:
                self.send_json({"error": "API endpoint not found"}, HTTPStatus.NOT_FOUND)
        except AppError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"Internal server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


    def serve_static(self, path: str) -> None:
        relative = "client.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        target = (CLIENT_ROOT / relative).resolve()
        if CLIENT_ROOT.resolve() not in target.parents and target != CLIENT_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            target = CLIENT_ROOT / "client.html"
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def serve_output(self, relative_path: str, source: str = "output") -> None:
        target = proof_image_path(relative_path, source)
        if target.suffix.lower() not in MEDIA_SUFFIXES:
            raise AppError("Only generated media files can be viewed")
        if not target.is_file():
            self.send_json({"error": "Output not found"}, HTTPStatus.NOT_FOUND)
            return
        total_size = target.stat().st_size
        start, end = 0, total_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or total_size == 0:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.end_headers()
                return
            requested_start, requested_end = match.groups()
            if requested_start:
                start = int(requested_start)
                end = int(requested_end) if requested_end else end
            else:
                suffix_size = int(requested_end or 0)
                start = max(0, total_size - suffix_size)
            if start >= total_size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.end_headers()
                return
            end = min(end, total_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        self.end_headers()
        with target.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def serve_thumbnail(self, name: str, source: str = "output") -> None:
        body = output_thumbnail(name, source)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def serve_preview_image(self, preview_id: str) -> None:
        body, content_type = WEB_STATE.preview_image(preview_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int, open_browser: bool) -> None:
    if not CLIENT_ROOT.joinpath("client.html").is_file():
        raise AppError(f"Web UI assets not found: {CLIENT_ROOT}")
    load_config()
    load_database()
    server = ThreadingHTTPServer((host, port), ValhallaHandler)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{server.server_port}/"
    label = "Gallery benchmark" if GALLERY_BENCHMARK_COUNT else "Valhalla Photo Studio Web UI"
    print(f"{label}: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Valhalla Photo Studio…")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valhalla Photo Studio local Web UI server")
    parser.add_argument(
        "command", nargs="?",
        choices=("serve", "validate", "stats", "gallery-benchmark"),
        default="serve",
        help=(
            "Start the production server, validate or summarize the production "
            "catalog, or run an isolated synthetic gallery benchmark"
        ),
    )
    parser.add_argument("--host", help="Override config.json listen_host for this run")
    parser.add_argument("--port", type=int, help="Override config.json listen_port for this run")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--count", type=int, default=2000, help="Synthetic gallery size in benchmark mode")
    return parser


def main() -> int:
    global GALLERY_BENCHMARK_COUNT
    args = build_parser().parse_args()
    try:
        config, _ = load_config()
        if args.command == "validate":
            database, _ = load_database()
            print_validation_report(validate_production_catalog(database))
            return 0
        if args.command == "stats":
            database, _ = load_database()
            print_catalog_statistics(catalog_statistics(database))
            return 0
        host = args.host if args.host is not None else config["server"]["host"]
        port = args.port if args.port is not None else config["server"]["port"]
        if not 1 <= port <= 65535:
            raise AppError("Listen port must be from 1 to 65535")
        if args.command == "gallery-benchmark":
            GALLERY_BENCHMARK_COUNT = _safe_int(
                args.count, "Gallery benchmark count", 101, 10_000
            )
            # Fail before binding the port rather than opening an unusable benchmark.
            list_output_images()
        serve(host, port, not args.no_browser)
        return 0
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not start web server: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
