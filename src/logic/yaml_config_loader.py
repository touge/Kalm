import os
import re
import yaml
from pathlib import Path
from typing import Any, Optional


class YamlConfigLoader:
    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = Path(config_path)
        self._data: dict = {}
        self.reload()

    def reload(self):
        if self._config_path.exists():
            with open(self._config_path, encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}

    def get(self, key_path: str, default: Any = None) -> Any:
        node = self._data
        for part in key_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return self._resolve_env(node)

    def _resolve_env(self, value: Any) -> Any:
        if isinstance(value, str):
            def _replace(m):
                var = m.group(1)
                return os.environ.get(var, m.group(0))
            return re.sub(r"\$\{(\w+)\}", _replace, value)
        if isinstance(value, dict):
            return {k: self._resolve_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_env(v) for v in value]
        return value


yaml_config_loader = YamlConfigLoader()
