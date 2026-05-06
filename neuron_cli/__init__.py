"""NeuronCLI - Python shim for the Rust binary."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_binary() -> Path:
    """Locate the embedded neuron binary inside the installed package."""
    pkg_dir = Path(__file__).resolve().parent
    binary_name = "neuron.exe" if sys.platform == "win32" else "neuron"
    candidate = pkg_dir / binary_name
    if candidate.exists():
        return candidate
    # Fallback: search PATH (useful during development)
    for path_dir in os.get_exec_path():
        candidate = Path(path_dir) / binary_name
        if candidate.exists():
            return candidate
    raise RuntimeError(
        f"NeuronCLI binary '{binary_name}' not found inside package or PATH. "
        "Try reinstalling: pip install --force-reinstall neuroncli"
    )


def main() -> None:
    """Invoke the Rust neuron binary with forwarded argv."""
    binary = _find_binary()
    # Replace sys.argv[0] with the actual binary path so the Rust CLI
    # sees correct program name in --version / help text.
    args = [str(binary), *sys.argv[1:]]
    os.execv(str(binary), args)


if __name__ == "__main__":
    main()
