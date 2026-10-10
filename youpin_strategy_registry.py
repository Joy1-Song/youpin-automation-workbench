from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


DEFAULT_STRATEGY_DIRS = ["strategies"]


def strategy_plugin_directories(config: dict[str, Any] | None = None) -> list[Path]:
    plugin_config = (config or {}).get("strategy_plugins", {})
    directories = plugin_config.get("directories", DEFAULT_STRATEGY_DIRS)
    if not isinstance(directories, list):
        directories = DEFAULT_STRATEGY_DIRS
    result: list[Path] = []
    for raw in directories:
        path = Path(str(raw))
        if not path.is_absolute():
            path = Path.cwd() / path
        result.append(path)
    return result


def load_strategy_module(path: Path) -> ModuleType | None:
    if not path.exists() or path.name.startswith("_") or path.suffix != ".py":
        return None
    spec = importlib.util.spec_from_file_location(f"youpin_desktop_strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_strategy_modules(config: dict[str, Any] | None = None) -> list[tuple[Path, ModuleType]]:
    modules: list[tuple[Path, ModuleType]] = []
    for directory in strategy_plugin_directories(config):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.py")):
            module = load_strategy_module(path)
            if module is not None:
                modules.append((path, module))
    return modules


def merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(target)
    for key, value in defaults.items():
        if isinstance(value, dict):
            child = merged.setdefault(key, {})
            if isinstance(child, dict):
                merged[key] = merge_defaults(child, value)
            else:
                merged[key] = copy.deepcopy(value)
        else:
            merged.setdefault(key, copy.deepcopy(value))
    return merged


def _module_default_config(module: ModuleType) -> dict[str, Any]:
    value = getattr(module, "DEFAULT_CONFIG", {})
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _raw_desktop_profile(module: ModuleType, config: dict[str, Any]) -> dict[str, Any]:
    getter = getattr(module, "get_desktop_profile", None)
    if callable(getter):
        value = getter(config)
    else:
        value = getattr(module, "DESKTOP_PROFILE", {})
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def desktop_profile_from_module(path: Path, module: ModuleType, config: dict[str, Any] | None = None) -> dict[str, Any]:
    plugin_id = path.stem
    plugin_mode = f"plugin:{plugin_id}"
    defaults = _module_default_config(module)
    raw = _raw_desktop_profile(module, config or {})
    label = str(raw.get("label") or getattr(module, "STRATEGY_NAME", plugin_id))
    note = str(raw.get("note") or getattr(module, "DESCRIPTION", ""))
    profile: dict[str, Any] = {
        "plugin_id": plugin_id,
        "label": label,
        "selection": raw.get("selection", plugin_mode),
        "bid": raw.get("bid", plugin_mode),
        "quantity": raw.get("quantity", plugin_mode),
        "create_channel": raw.get("create_channel", "emulator_queue"),
        "allow_create": raw.get("allow_create", True),
        "note": note,
        "module_path": str(path),
        "config_defaults": defaults,
    }

    quantity = defaults.get("quantity_strategy", {}) if isinstance(defaults.get("quantity_strategy"), dict) else {}
    risk = defaults.get("purchase_risk", {}) if isinstance(defaults.get("purchase_risk"), dict) else {}
    bid = defaults.get("bid_strategy", {}) if isinstance(defaults.get("bid_strategy"), dict) else {}
    for key, default_value in {
        "top_n": defaults.get("top_n"),
        "budget": quantity.get("budget", bid.get("max_daily_spend")),
        "min_quantity_per_item": quantity.get("min_quantity_per_item"),
        "max_quantity_per_item": quantity.get("max_quantity_per_item"),
        "max_quantity_hard_limit": None,
        "active_order_exposure_multiplier": risk.get("active_order_exposure_multiplier"),
    }.items():
        value = raw.get(key, default_value)
        if value is not None:
            profile[key] = value
    return profile


def discover_desktop_profiles(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for path, module in discover_strategy_modules(config):
        if not hasattr(module, "DESKTOP_PROFILE") and not callable(getattr(module, "get_desktop_profile", None)):
            continue
        profile = desktop_profile_from_module(path, module, config)
        label = str(profile.get("label") or path.stem)
        profiles[label] = profile
    return profiles
