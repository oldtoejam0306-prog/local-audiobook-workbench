"""Load machine-specific paths and SSH settings from an external JSON file."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import sys
from typing import Any


def load_config() -> dict[str, Any]:
    filename = os.environ.get("AUDIOBOOK_CONFIG_FILE")
    if not filename:
        return {}
    path = Path(filename).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AUDIOBOOK_CONFIG_FILE could not be read") from exc
    if not isinstance(value, dict):
        raise RuntimeError("configuration root must be an object")
    return value


def section(name: str) -> dict[str, Any]:
    value = load_config().get(name, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"configuration section {name!r} must be an object")
    return value


def configured_path(section_name: str, key: str, env_name: str, default: Path | None = None) -> Path:
    configured = os.environ.get(env_name) or section(section_name).get(key)
    if configured:
        return Path(str(configured)).expanduser()
    if default is not None:
        return default.expanduser()
    raise RuntimeError(f"configure {env_name} or {section_name}.{key}")


def local_data_root() -> Path:
    return configured_path("local", "data_root", "AUDIOBOOK_DATA_ROOT", Path.home() / "AudiobookWorkbench" / "data")


def local_workbench_root() -> Path:
    return configured_path("workbench", "data_root", "AUDIOBOOK_WORKBENCH_ROOT", local_data_root() / "real-workbench")


def mac_data_root() -> Path:
    return configured_path("mac", "data_root", "AUDIOBOOK_DATA_ROOT", Path.home() / "AudiobookWorkbench")


def huashuo_root() -> Path:
    return configured_path("mac", "huashuo_root", "HUASHUO_ROOT", Path.home() / "Huashuo")


def mac_huashuo_cli() -> Path:
    return configured_path("mac", "huashuo_cli", "AUDIOBOOK_HUASHUO_CLI", Path(shutil.which("huashuo") or "huashuo"))


def hf_home() -> Path:
    return mac_customvoice_hf_home()


def sandbox_exec() -> Path:
    return configured_path("mac", "sandbox_exec", "AUDIOBOOK_SANDBOX_EXEC", Path("/usr/bin/sandbox-exec"))


def mac_stage_root() -> Path:
    return configured_path("mac", "stage_root", "AUDIOBOOK_STAGE_ROOT", mac_data_root() / "staging")


def mac_voice_root() -> Path:
    return configured_path("mac", "voice_stage_root", "AUDIOBOOK_VOICE_STAGE_ROOT", mac_stage_root() / "voices")


def mac_model_root() -> Path:
    return mac_voice_design_model_root()


def mac_voice_design_model_root() -> Path:
    configured = os.environ.get("AUDIOBOOK_VOICE_DESIGN_MODEL_ROOT") or section("mac").get("voice_design_model_root")
    if not configured:
        models = section("mac").get("models", {})
        configured = models.get("voice_design", {}).get("snapshot_root") if isinstance(models, dict) else None
    if not isinstance(configured, str) or not configured.strip():
        raise RuntimeError("configure mac.voice_design_model_root or mac.models.voice_design.snapshot_root")
    return Path(configured).expanduser()


def mac_customvoice_hf_home() -> Path:
    return configured_path("mac", "customvoice_hf_home", "AUDIOBOOK_CUSTOMVOICE_HF_HOME", Path.home() / ".cache" / "huggingface")


def validate_customvoice_hf_home(value: str | Path | None = None) -> Path:
    """Require a full Hugging Face cache root containing model snapshots."""
    home = Path(value).expanduser() if value is not None else mac_customvoice_hf_home()
    try:
        home = home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("configured CustomVoice HF cache root is unavailable") from exc
    hub = home / "hub"
    snapshots = list(hub.glob("models--*/snapshots/*")) if hub.is_dir() else []
    if not any(item.is_dir() and not item.is_symlink() for item in snapshots):
        raise RuntimeError("configured CustomVoice path must be a full HF_HOME cache containing hub/models--*/snapshots/*")
    return home


def mac_voice_library_root() -> Path:
    return configured_path("mac", "voice_root", "AUDIOBOOK_VOICE_ROOT", mac_data_root() / "voice-library")


def mac_controlled_root() -> Path:
    value = os.environ.get("AUDIOBOOK_CONTROLLED_REAL_ROOT") or section("mac").get("controlled_root")
    if value:
        return Path(str(value)).expanduser()
    return (mac_data_root() / "controlled-real").expanduser()


def mac_controlled_voice() -> dict[str, Any]:
    value = section("mac").get("controlled_voice")
    if not isinstance(value, dict) or set(value) != {"stage", "vector_sha256"}:
        raise RuntimeError("configure mac.controlled_voice.stage and vector_sha256")
    stage = Path(str(value["stage"])).expanduser()
    vector_sha256 = value["vector_sha256"]
    if not isinstance(vector_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", vector_sha256):
        raise RuntimeError("mac.controlled_voice.vector_sha256 must be a lowercase SHA-256")
    return {"stage": stage, "vector_sha256": vector_sha256}


def mac_multivoice_voices() -> list[dict[str, Any]]:
    values = section("mac").get("multivoice_voices", [])
    if not isinstance(values, list):
        raise RuntimeError("mac.multivoice_voices must be a list")
    checked = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"role", "stage", "vector_sha256"}:
            raise RuntimeError("each mac.multivoice_voices entry needs role, stage, and vector_sha256")
        role, stage, digest = value["role"], Path(str(value["stage"])).expanduser(), value["vector_sha256"]
        if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", role) or role in seen:
            raise RuntimeError("multivoice roles must be unique safe identifiers")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("multivoice vector SHA-256 is invalid")
        seen.add(role)
        checked.append({"role": role, "stage": stage, "vector_sha256": digest})
    return checked


def mac_presets_path() -> Path:
    return configured_path("mac", "presets_path", "AUDIOBOOK_PRESETS_PATH")


def mac_ffmpeg() -> Path:
    configured = os.environ.get("AUDIOBOOK_FFMPEG") or section("mac").get("ffmpeg")
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            resolved = shutil.which(str(configured))
            if resolved:
                return Path(resolved).resolve(strict=True)
        return path
    raise RuntimeError("configure AUDIOBOOK_FFMPEG or mac.ffmpeg")


def _resolve_media_tool(value: str | Path, label: str, search_path: str | None = None) -> Path:
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(str(value), path=search_path)
        if located:
            candidate = Path(located)
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"configured {label} is unavailable") from exc
    if not candidate.is_file(): raise RuntimeError(f"configured {label} is not a regular file")
    return candidate


def mac_ffprobe(search_path: str | None = None) -> Path:
    configured = os.environ.get("AUDIOBOOK_FFPROBE") or section("mac").get("ffprobe")
    if configured:
        return _resolve_media_tool(str(configured), "FFprobe", search_path)
    ffmpeg = _resolve_media_tool(mac_ffmpeg(), "FFmpeg", search_path)
    sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if sibling.is_file(): return sibling.resolve(strict=True)
    located = shutil.which("ffprobe", path=search_path)
    if located:
        return _resolve_media_tool(located, "FFprobe", search_path)
    raise RuntimeError("configure mac.ffprobe or AUDIOBOOK_FFPROBE, or add FFprobe beside FFmpeg/PATH")


def validate_media_tools(search_path: str | None = None) -> dict[str, Path]:
    """Fail before any model work if either bundled multimedia binary is missing."""
    effective_path = os.environ.get("PATH") if search_path is None else search_path
    ffmpeg = _resolve_media_tool(mac_ffmpeg(), "FFmpeg", effective_path)
    ffprobe = mac_ffprobe(effective_path)
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe}


def ffmpeg_search_path(current_path: str | None = None) -> str:
    tools = validate_media_tools(current_path)
    directories = list(dict.fromkeys((str(tools["ffmpeg"].parent), str(tools["ffprobe"].parent))))
    if current_path: directories.append(current_path)
    return os.pathsep.join(filter(None, directories))


def model_config(role: str) -> dict[str, Any]:
    models = section("mac").get("models", {})
    value = models.get(role) if isinstance(models, dict) else None
    required = {"role", "model_id", "repository_root", "snapshot_root", "inventory_sha256"}
    if not isinstance(value, dict) or set(value) != required or value.get("role") != role:
        raise RuntimeError(f"configure mac.models.{role} with the required model inventory fields")
    return value


def resource_limits() -> dict[str, Any]:
    value = section("mac").get("resource_limits")
    required = {"min_pressure_free_percent", "min_available_bytes", "max_child_rss_bytes", "poll_seconds"}
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("configure mac.resource_limits with the orchestrator fields")
    if (type(value["min_pressure_free_percent"]) is not int or not 1 <= value["min_pressure_free_percent"] <= 100
            or type(value["min_available_bytes"]) is not int or value["min_available_bytes"] <= 0
            or type(value["max_child_rss_bytes"]) is not int or value["max_child_rss_bytes"] <= 0
            or type(value["poll_seconds"]) not in (int, float) or not 0.1 <= value["poll_seconds"] <= 10):
        raise RuntimeError("mac.resource_limits contains invalid values")
    return value


def apply_mac_environment() -> None:
    """Expose validated machine-local settings to isolated child processes."""
    media = validate_media_tools()
    os.environ["AUDIOBOOK_DATA_ROOT"] = str(mac_data_root())
    os.environ["AUDIOBOOK_STAGE_ROOT"] = str(mac_stage_root())
    os.environ["AUDIOBOOK_VOICE_STAGE_ROOT"] = str(mac_voice_root())
    os.environ["AUDIOBOOK_VOICE_ROOT"] = str(mac_voice_library_root())
    os.environ["AUDIOBOOK_PRESETS_PATH"] = str(mac_presets_path())
    os.environ["HUASHUO_ROOT"] = str(huashuo_root())
    os.environ["AUDIOBOOK_HUASHUO_CLI"] = str(mac_huashuo_cli())
    os.environ["AUDIOBOOK_VOICE_DESIGN_MODEL_ROOT"] = str(mac_voice_design_model_root())
    os.environ["AUDIOBOOK_CUSTOMVOICE_HF_HOME"] = str(mac_customvoice_hf_home())
    os.environ["HF_HOME"] = str(mac_customvoice_hf_home())
    os.environ["AUDIOBOOK_FFMPEG"] = str(media["ffmpeg"])
    os.environ["AUDIOBOOK_FFPROBE"] = str(media["ffprobe"])
    os.environ["AUDIOBOOK_SANDBOX_EXEC"] = str(sandbox_exec())
    os.environ["AUDIOBOOK_PYTHON"] = str(Path(os.path.abspath(sys.executable)))
    os.environ["AUDIOBOOK_CONTROLLED_REAL_ROOT"] = str(mac_controlled_root())


def ssh_target() -> str:
    value = os.environ.get("AUDIOBOOK_SSH_TARGET") or section("remote").get("target")
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
        raise RuntimeError("configure AUDIOBOOK_SSH_TARGET or remote.target as an SSH host alias")
    return value


def ssh_options() -> list[str]:
    remote = section("remote")
    known_hosts = os.environ.get("AUDIOBOOK_SSH_KNOWN_HOSTS") or remote.get("known_hosts_file")
    if not isinstance(known_hosts, str) or not known_hosts.strip():
        raise RuntimeError("configure AUDIOBOOK_SSH_KNOWN_HOSTS or remote.known_hosts_file")
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
              "-o", f"UserKnownHostsFile={Path(known_hosts).expanduser()}"]
    key = os.environ.get("AUDIOBOOK_SSH_KEY") or remote.get("key_file")
    if isinstance(key, str) and key.strip():
        options.extend(["-i", str(Path(key).expanduser())])
    return options


def remote_root() -> PurePosixPath:
    remote = section("remote")
    value = os.environ.get("AUDIOBOOK_REMOTE_ROOT") or remote.get("project_root")
    if not isinstance(value, str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("configure remote.project_root as an absolute POSIX directory")
    return PurePosixPath(value)


def remote_stage_root() -> PurePosixPath:
    remote = section("remote")
    value = os.environ.get("AUDIOBOOK_REMOTE_STAGE_ROOT") or remote.get("stage_root")
    if not isinstance(value, str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("configure remote.stage_root as an absolute POSIX directory")
    return PurePosixPath(value)


def remote_voice_root() -> PurePosixPath:
    remote = section("remote")
    value = os.environ.get("AUDIOBOOK_REMOTE_VOICE_ROOT") or remote.get("voice_root")
    if not isinstance(value, str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("configure remote.voice_root as an absolute POSIX directory")
    return PurePosixPath(value)


def remote_legacy_chapter_root() -> PurePosixPath:
    remote = section("remote")
    value = os.environ.get("AUDIOBOOK_REMOTE_LEGACY_CHAPTER_ROOT") or remote.get("legacy_chapter_root")
    if not isinstance(value, str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("configure remote.legacy_chapter_root to inspect existing chapter jobs")
    return PurePosixPath(value)


def legacy_chapter_mode() -> str:
    remote = section("remote")
    mode = os.environ.get("AUDIOBOOK_LEGACY_CHAPTER_MODE") or remote.get("legacy_mode")
    if mode == "root":
        remote_legacy_chapter_root()
        return "root"
    acknowledged = section("workbench_v2").get("single_user_ack") is True
    if mode == "none" and acknowledged:
        return "none"
    raise RuntimeError("configure remote.legacy_chapter_root or explicitly set remote.legacy_mode='none' and workbench_v2.single_user_ack=true")


def remote_controlled_root() -> PurePosixPath:
    remote = section("remote")
    value = os.environ.get("AUDIOBOOK_REMOTE_CONTROLLED_ROOT") or remote.get("controlled_root")
    if not isinstance(value, str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("configure remote.controlled_root as an absolute POSIX directory")
    return PurePosixPath(value)


def remote_script(name: str) -> PurePosixPath:
    if not name.endswith(".py") or "/" in name or "\\" in name:
        raise ValueError("remote worker must be a Python filename")
    return remote_root() / name


def remote_command(name: str) -> str:
    remote = section("remote")
    python = os.environ.get("AUDIOBOOK_REMOTE_PYTHON") or remote.get("python", "python3")
    config_file = os.environ.get("AUDIOBOOK_REMOTE_CONFIG_FILE") or remote.get("config_file")
    if not isinstance(python, str) or not python.strip():
        raise RuntimeError("configure remote.python")
    if not isinstance(config_file, str) or not config_file.strip():
        raise RuntimeError("configure AUDIOBOOK_REMOTE_CONFIG_FILE or remote.config_file")
    parts = ["env", "AUDIOBOOK_CONFIG_FILE=" + str(config_file), python, str(remote_script(name))]
    return " ".join(shlex.quote(part) for part in parts)


def remote_path_is_within(value: str, root: PurePosixPath) -> bool:
    candidate = PurePosixPath(value)
    return candidate.is_absolute() and ".." not in candidate.parts and candidate != root and root in candidate.parents


def scp_source(value: str) -> str:
    return f"{ssh_target()}:{shlex.quote(value)}"
