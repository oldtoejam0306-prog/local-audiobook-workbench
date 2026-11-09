"""Idempotent cooperative multi-voice batch controller with conservative locks."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import release_config


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_state(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _group_exists(pgid: int) -> bool:
    if os.name != "posix":
        return True
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _stop_group(pgid: int, grace_seconds: float = 8.0) -> bool:
    """Stop only the process group started for this controller child."""
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return True
        time.sleep(0.1)
    return not _group_exists(pgid)


def _run_worker(args: list[str], timeout: int) -> tuple[int | None, str, str, bool]:
    """Run one worker in a new POSIX process group and confirm descendants exit."""
    if os.name != "posix":
        raise RuntimeError("multi-voice workers require a POSIX process-group supervisor")
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", start_new_session=True, close_fds=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        group_gone = _stop_group(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
            group_gone = False
        return process.returncode, stdout, stderr, group_gone
    except BaseException:
        group_gone = _stop_group(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
            group_gone = False
        return process.returncode, stdout, stderr, group_gone
    group_gone = not _group_exists(process.pid)
    if not group_gone:
        group_gone = _stop_group(process.pid)
    return process.returncode, stdout, stderr, group_gone


def _job_identity(input_bytes: bytes, voices: list[dict], redo_nonce: str | None = None) -> tuple[str, str, str, str | None]:
    input_sha = digest(input_bytes)
    pinned = [{"role": item["role"], "vector_sha256": item["vector_sha256"]} for item in voices]
    voice_sha = digest(json.dumps(pinned, sort_keys=True, separators=(",", ":")).encode())
    nonce_sha = digest(redo_nonce.encode("utf-8")) if redo_nonce is not None else None
    job_sha = digest((input_sha + "\0" + voice_sha + "\0" + (nonce_sha or "")).encode())
    return job_sha, input_sha, voice_sha, nonce_sha


def _existing_state(state_path: Path, job_sha: str) -> dict | None:
    if not state_path.exists():
        return None
    if state_path.is_symlink():
        raise ValueError("controller state may not be a symlink")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("job_sha256") != job_sha:
        raise ValueError("controller state identity mismatch")
    return state


def run_controller(input_path: str, worker_name: str = "isolated_real_multivoice_mac.py", max_batches: int = 50,
                   redo_nonce: str | None = None) -> dict:
    if worker_name not in ("isolated_real_multivoice_mac.py", "isolated_real_multivoice_roles_mac.py"):
        raise ValueError("unsupported multi-voice worker")
    input_file = Path(input_path).expanduser()
    if input_file.is_symlink() or not input_file.is_file() or not input_file.resolve(strict=True).is_relative_to(
        release_config.mac_data_root().resolve(strict=True)
    ):
        raise ValueError("input must be a regular file under the configured data root")
    if type(max_batches) is not int or not 1 <= max_batches <= 50:
        raise ValueError("max_batches must be between 1 and 50")
    if redo_nonce is not None and (not isinstance(redo_nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", redo_nonce)):
        raise ValueError("explicit redo nonce must be a fresh 16-128 character identifier")
    voices = release_config.mac_multivoice_voices()
    if not voices:
        raise ValueError("no voice stages are configured")
    job_sha, input_sha, voice_sha, nonce_sha = _job_identity(input_file.read_bytes(), voices, redo_nonce)
    controlled_root = release_config.mac_controlled_root().resolve(strict=False)
    run_root = controlled_root / "multivoice"
    jobs_root = run_root / "jobs"
    state_path = jobs_root / job_sha / "controller.json"
    existing = _existing_state(state_path, job_sha) if state_path.exists() else None
    if existing:
        status = existing.get("status")
        if status == "complete":
            return {"status": "already_complete", "retry_allowed": False, "job_sha256": job_sha}
        return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
    lock = controlled_root / "active.lock"
    try:
        lock.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return {"status": "busy_or_uncertain", "retry_allowed": False, "job_sha256": job_sha}
    lock_safe_to_release = True
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "status": "reserved", "retry_allowed": False, "job_sha256": job_sha,
                 "input_sha256": input_sha, "voice_inventory_sha256": voice_sha,
                 "redo_nonce_sha256": nonce_sha, "batches": []}
        try:
            with state_path.open("x", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
        except FileExistsError:
            return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
        worker = Path(__file__).resolve().with_name(worker_name)
        interpreter = Path(os.path.abspath(sys.executable))
        prior = None
        for _ in range(max_batches):
            state["status"] = "running"
            _write_state(state_path, state)
            args = [str(interpreter), str(worker), str(input_file.resolve(strict=True))]
            if prior is not None:
                args.append(str(prior))
            try:
                returncode, stdout, stderr, group_gone = _run_worker(args, timeout=800)
            except Exception:
                state.update(status="failed", reason="worker process could not be started or supervised")
                _write_state(state_path, state)
                return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
            if not group_gone:
                lock_safe_to_release = False
                state.update(status="unknown", reason="worker process group could not be confirmed stopped")
                _write_state(state_path, state)
                return {"status": "unknown", "retry_allowed": False, "job_sha256": job_sha}
            if returncode != 0:
                state.update(status="failed", reason="worker failed after its process group exited")
                _write_state(state_path, state)
                return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
            try:
                response = json.loads(stdout.strip().splitlines()[-1])
                current = Path(response["run"]).resolve(strict=True)
            except Exception as exc:
                state.update(status="failed", reason="worker returned invalid metadata")
                _write_state(state_path, state)
                raise RuntimeError("multi-voice worker returned invalid batch metadata") from exc
            if current.parent != run_root.resolve(strict=True):
                state.update(status="failed", reason="worker output escaped configured run root")
                _write_state(state_path, state)
                raise RuntimeError("multi-voice worker escaped its run directory")
            model_hash = response.get("model_inventory_sha256")
            if not isinstance(model_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", model_hash):
                state.update(status="failed", reason="worker did not return a pinned model inventory")
                _write_state(state_path, state)
                return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
            if state.get("model_inventory_sha256") and state["model_inventory_sha256"] != model_hash:
                state.update(status="requires_review", reason="model inventory changed between batches")
                _write_state(state_path, state)
                return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha}
            state["model_inventory_sha256"] = model_hash
            stages = list(current.glob("huashuo-isolated-*"))
            if len(stages) != 1 or stages[0].is_symlink():
                state.update(status="failed", reason="isolated stage missing or ambiguous")
                _write_state(state_path, state)
                raise RuntimeError("isolated Huashuo stage is missing or ambiguous")
            stage = stages[0].resolve(strict=True)
            if stage.parent != current:
                state.update(status="failed", reason="isolated stage escaped run directory")
                _write_state(state_path, state)
                raise RuntimeError("isolated stage escaped its run directory")
            before_path, after_path = stage / "batch-before.json", stage / "batch-after.json"
            before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.is_file() else None
            after = json.loads(after_path.read_text(encoding="utf-8")) if after_path.is_file() else None
            model_copy = stage / "hf"
            if model_copy.is_dir() and not model_copy.is_symlink() and model_copy.resolve().parent == stage:
                shutil.rmtree(model_copy)
            status = response.get("status")
            state["batches"].append({"run_name": current.name, "status": status,
                                     "cached": after.get("cached") if after else None,
                                     "total": after.get("total") if after else None})
            state["last_run_name"] = current.name
            if status == "complete":
                state["status"] = "complete"
                _write_state(state_path, state)
                return {"status": "complete", "retry_allowed": False, "job_sha256": job_sha,
                        "batches": len(state["batches"])}
            if status != "batch_pending" or not before or not after or after.get("cached", -1) <= before.get("cached", -1):
                state.update(status="requires_review", reason="batch made no verified cache progress")
                _write_state(state_path, state)
                return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha,
                        "batches": len(state["batches"])}
            prior = current
        state.update(status="requires_review", reason="configured batch limit reached")
        _write_state(state_path, state)
        return {"status": "requires_review", "retry_allowed": False, "job_sha256": job_sha,
                "batches": len(state["batches"])}
    finally:
        if lock_safe_to_release:
            try:
                lock.rmdir()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json")
    parser.add_argument("--worker", choices=("isolated_real_multivoice_mac.py", "isolated_real_multivoice_roles_mac.py"),
                        default="isolated_real_multivoice_mac.py")
    parser.add_argument("--redo-nonce", help="explicitly start a new immutable attempt with a fresh operator-chosen nonce")
    args = parser.parse_args()
    result = run_controller(args.input_json, args.worker, redo_nonce=args.redo_nonce)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
