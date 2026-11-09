"""Cooperative continuation entry point for configured multi-role voices."""
from __future__ import annotations

import json
import sys

from isolated_real_batch_controller import run_controller


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError("usage: isolated_real_roles_controller.py INPUT_JSON")
    result = run_controller(sys.argv[1], "isolated_real_multivoice_roles_mac.py")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
