"""Audit all candidate files, including hidden files, without printing matched content."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parent
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_assignment": re.compile(rb'(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[=:]\s*["\x27][A-Za-z0-9_\-]{20,}'),
    "ipv4": re.compile(rb'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
    "personal_path": re.compile(rb'(?i)(?:[a-z]:[\\/](?:users|codexshared|codexworkspaces)[\\/]|/' + b'Users' + rb'/[^/\s]+/)'),
}
FORBIDDEN_SUFFIXES = {'.wav', '.mp3', '.flac', '.safetensors', '.pt', '.pth', '.ckpt', '.gguf', '.npy', '.npz', '.pyc'}
findings = {k: [] for k in (*PATTERNS, 'forbidden_artifact', 'git_metadata', 'unreadable')}
files = 0
for path in ROOT.rglob('*'):
    if not path.is_file():
        continue
    rel = path.relative_to(ROOT).as_posix()
    files += 1
    if '.git' in path.relative_to(ROOT).parts:
        findings['git_metadata'].append(rel)
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        findings['forbidden_artifact'].append(rel)
    try:
        data = path.read_bytes()
    except OSError:
        findings['unreadable'].append(rel)
        continue
    for key, pattern in PATTERNS.items():
        matches = pattern.findall(data)
        if key == "ipv4":
            matches = [m for m in matches if m != b"127.0.0.1"]
        if matches:
            findings[key].append(rel)
print(json.dumps({'files_scanned': files, 'findings': findings}, indent=2))
