"""
config_loader.py - merges config/base.yaml with config/<env>.yaml.

Usage:
    from config_loader import load_config
    cfg = load_config(env="local")   # or env="fox"
    cfg["ks4_params"]["Th_universal"]
    cfg["paths"]["base_path"]

Requires PyYAML (pip install pyyaml / conda install pyyaml).
"""

import os
import copy
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _deep_merge(base, override):
    """Recursively merge override into base. override wins on conflicts."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _expand_env_vars(obj):
    """Recursively expand ${VAR} style environment variables in string values -
    needed for fox.yaml's ${LOCALSCRATCH} etc."""
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    return obj


def load_config(env: str, config_dir: Path = CONFIG_DIR) -> dict:
    """
    Load base.yaml and merge in <env>.yaml (env-specific paths override/extend
    base). Environment variables in path strings (e.g. ${LOCALSCRATCH}) are
    expanded after merging.
    """
    base_path = config_dir / "base.yaml"
    env_path = config_dir / f"{env}.yaml"

    if not base_path.exists():
        raise FileNotFoundError(f"base.yaml not found at {base_path}")
    if not env_path.exists():
        raise FileNotFoundError(
            f"Environment config not found: {env_path}. "
            f"Expected one of the files in {config_dir} matching '<env>.yaml' "
            f"(e.g. 'local.yaml', 'fox.yaml').")

    with open(base_path) as f:
        base_cfg = yaml.safe_load(f) or {}
    with open(env_path) as f:
        env_cfg = yaml.safe_load(f) or {}

    merged = _deep_merge(base_cfg, env_cfg)
    merged = _expand_env_vars(merged)
    merged["_env"] = env  # so downstream code can log/branch on which env is active
    return merged


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print the merged config for a given environment (debugging aid).")
    parser.add_argument("--env", required=True, choices=["local", "fox"])
    args = parser.parse_args()

    cfg = load_config(args.env)
    print(json.dumps(cfg, indent=2, default=str))
