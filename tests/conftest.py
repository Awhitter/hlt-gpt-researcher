"""Load the standalone HLT SQLite module without adding service paths.

The container exposes /app; tests load service modules by filename instead.
Register only this dependency, so services cannot shadow unrelated packages.
"""

import importlib.util
import sys
from pathlib import Path

_source = Path(__file__).resolve().parents[1] / "services/agent/hlt_sqlite.py"
_spec = importlib.util.spec_from_file_location("hlt_sqlite", _source)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
sys.modules["hlt_sqlite"] = _module

_source = Path(__file__).resolve().parents[1] / "services/agent/hlt_provider_recovery.py"
_spec = importlib.util.spec_from_file_location("hlt_provider_recovery", _source)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
sys.modules["hlt_provider_recovery"] = _module

_source = Path(__file__).resolve().parents[1] / "services/agent/hlt_artifact_run_context.py"
_spec = importlib.util.spec_from_file_location("hlt_artifact_run_context", _source)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
sys.modules["hlt_artifact_run_context"] = _module
