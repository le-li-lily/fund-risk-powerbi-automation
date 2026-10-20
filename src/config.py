import json
from pathlib import Path
from typing import Union


def load_config(path: Union[str, Path]) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
