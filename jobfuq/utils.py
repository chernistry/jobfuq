# jobfuq/utils.py

from typing import Any, Dict
import sys

try:
    import tomllib  # For Python 3.11+
except ImportError:
    import tomli as tomllib  # For Python < 3.11

def load_config(config_path: str) -> Dict[str, Any]:
    """
    Load a TOML configuration file.

    Args:
        config_path (str): Path to the TOML configuration file.

    Returns:
        Dict[str, Any]: A dictionary containing configuration parameters.
    """
    with open(config_path, "rb") as f:
        return tomllib.load(f)
