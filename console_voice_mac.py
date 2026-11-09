"""One-shot experimental fictional voice generation on the configured Mac."""
import hashlib, json, os, sys
from pathlib import Path
import release_config

BASE = release_config.mac_stage_root()
ORCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCH))
import experimental_mac_voice_orchestrator as orchestrator

def main():
    release_config.apply_mac_environment()
    request = json.load(sys.stdin)
    if set(request) != {'description', 'text', 'seed'}:
        raise ValueError('invalid request')
    design = release_config.model_config('voice_design')
    base = release_config.model_config('speaker_encoder')
    python = Path(os.path.abspath(sys.executable))
    python_sha256 = hashlib.sha256(python.read_bytes()).hexdigest()
    limits = release_config.resource_limits()
    result = orchestrator.run_experiment(
        parent=release_config.mac_voice_root(), description=request['description'], text=request['text'], seed=request['seed'],
        design_model=orchestrator._model_from(design), base_model=orchestrator._model_from(base),
        python_path=str(python), python_sha256=python_sha256, resource_limits=limits, experiment_enabled=True)
    print(json.dumps({'ok': True, 'stage': result['stage'], 'wav_sha256': result['wav_sha256'],
                      'vector_sha256': result['vector_sha256']}, ensure_ascii=False))

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
