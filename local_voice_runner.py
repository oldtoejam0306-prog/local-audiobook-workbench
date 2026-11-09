"""Synthetic-only local voice runner contract; real execution is disabled.

This module performs no model loading, network or file writes. Model inventories
must be acquired from caller-controlled local directories. Their hashes do not
authenticate those directories, prove model quality, or prove a future engine
actually loaded them. A real runner requires a separately authenticated macOS
sandbox launcher and an immutable, fully bound multi-file inventory.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import local_model_inventory
from isolated_voice_design import _validate_vector, _validate_wav


class VoiceRunnerBlocked(ValueError):
    pass


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def inventory_sha256(value: Mapping[str, Any]) -> str:
    return _hash(_json(value))


@dataclass(frozen=True)
class LocalModel:
    """The caller pins the inventory digest and controls the repository boundary."""
    role: str
    model_id: str
    repository_root: Path
    snapshot_root: Path
    inventory_sha256: str


class SyntheticEngine(Protocol):
    def design(self, request: Mapping[str, Any]) -> bytes: ...
    def encode(self, generated_wav: bytes) -> bytes: ...


def _inspect_model(model: LocalModel, role: str) -> dict[str, Any]:
    if not isinstance(model, LocalModel) or model.role != role:
        raise VoiceRunnerBlocked(f'{role} model identity is required')
    if (not isinstance(model.model_id, str) or not model.model_id.strip() or '://' in model.model_id
            or not isinstance(model.inventory_sha256, str) or len(model.inventory_sha256) != 64
            or any(c not in '0123456789abcdef' for c in model.inventory_sha256)):
        raise VoiceRunnerBlocked(f'{role} model identifier or inventory pin is invalid')
    try:
        result = local_model_inventory.inventory_model(model.repository_root, model.snapshot_root)
    except (local_model_inventory.ModelInventoryError, OSError) as exc:
        raise VoiceRunnerBlocked(f'{role} local model inventory is invalid') from exc
    if inventory_sha256(result) != model.inventory_sha256:
        raise VoiceRunnerBlocked(f'{role} local model inventory does not match its pin')
    return result


def _request(description: str, sample_text: str, seed: int) -> dict[str, Any]:
    for value, name, limit in ((description, 'description', 2000), (sample_text, 'sample_text', 500)):
        if (not isinstance(value, str) or not value.strip() or len(value) > limit
                or any(not ch.isprintable() and ch not in '\n\t' for ch in value)):
            raise VoiceRunnerBlocked(f'{name} must be bounded fictional text')
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        raise VoiceRunnerBlocked('seed must be an unsigned 32-bit integer')
    return {'description': description, 'sample_text': sample_text, 'seed': seed,
            'fictional_voice_only': True, 'reference_audio': False}


class LocalVoiceRunner:
    """Contract skeleton only. Do not use synthetic results for real rendering."""
    def __init__(self, *, design_model: LocalModel, encoder_model: LocalModel):
        if not isinstance(design_model, LocalModel) or not isinstance(encoder_model, LocalModel):
            raise VoiceRunnerBlocked('two local model identities are required')
        self._design_model = design_model
        self._encoder_model = encoder_model

    def run_real(self, *, description: str, sample_text: str, seed: int) -> None:
        raise VoiceRunnerBlocked('real runner disabled until trusted macOS sandbox and multi-file model binding exist')

    def run_synthetic(self, *, description: str, sample_text: str, seed: int,
                      engine: SyntheticEngine) -> dict[str, Any]:
        """Only for trusted injected tests, not a security boundary or production route.

        The interface has no caller WAV/reference argument. The encoder gets the
        exact bytes returned by the immediately preceding design call. A malicious
        injected engine can violate this convention; never inject a real engine.
        """
        request = _request(description, sample_text, seed)
        design_inventory = _inspect_model(self._design_model, 'voice_design')
        encoder_inventory = _inspect_model(self._encoder_model, 'speaker_encoder')
        if not callable(getattr(engine, 'design', None)) or not callable(getattr(engine, 'encode', None)):
            raise VoiceRunnerBlocked('synthetic engine must provide design and encode')
        wav = engine.design(copy.deepcopy(request))
        if not isinstance(wav, bytes):
            raise VoiceRunnerBlocked('synthetic design must return WAV bytes')
        try:
            _validate_wav(wav)
        except ValueError as exc:
            raise VoiceRunnerBlocked('synthetic design returned invalid WAV') from exc
        wav_sha = _hash(wav)
        vector = engine.encode(wav)
        if not isinstance(vector, bytes):
            raise VoiceRunnerBlocked('synthetic encoder must return NPY bytes')
        try:
            _validate_vector(vector)
        except ValueError as exc:
            raise VoiceRunnerBlocked('synthetic encoder returned invalid vector') from exc
        # Detect ordinary modifications while a fake engine was running. This is
        # not an atomic filesystem snapshot and does not stop adversarial races.
        if (_inspect_model(self._design_model, 'voice_design') != design_inventory
                or _inspect_model(self._encoder_model, 'speaker_encoder') != encoder_inventory):
            raise VoiceRunnerBlocked('model inventory changed during the run')
        return {'mode': 'synthetic_test_only', 'request': request, 'request_sha256': _hash(_json(request)),
                'design_model_id': self._design_model.model_id,
                'design_inventory_sha256': self._design_model.inventory_sha256,
                'encoder_model_id': self._encoder_model.model_id,
                'encoder_inventory_sha256': self._encoder_model.inventory_sha256,
                'generated_wav': wav, 'generated_wav_sha256': wav_sha,
                'speaker_vector_npy': vector, 'speaker_vector_sha256': _hash(vector),
                'human_reviewed': False}
