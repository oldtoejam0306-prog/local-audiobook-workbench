# Third-party notices

This source distribution uses external runtime dependencies and services. Install them separately and retain their respective notices.

## Runtime components

- Huashuo: upstream repository `wixette/huashuo`, Apache License 2.0. This candidate imports the installed package/CLI and does not copy its source.
- MLX and MLX Audio: upstream projects identify the MIT License. They are used by the Mac-side voice experiment when installed separately.
- NumPy: BSD-3-Clause.
- `python-docx` 1.2.0: MIT License. It may install `lxml` (BSD-3-Clause) and `typing_extensions` (PSF License).
- FFmpeg: invoked as an installed system tool; no binary is included here. Check the license and build options of the installed distribution.
- DeepSeek: remote API service, not a bundled code dependency. When a user runs the single-chapter workers, chapter text and requested context are sent to the configured service. Review the service terms and the user’s authorization before use.

The Qwen3-TTS source repository identifies Apache-2.0. Individual model card and weight terms must be checked for each selected model/version; no model weight is included or licensed by this repository.

This repository's Apache-2.0 LICENSE applies to the distributed project source. It does not grant rights to third-party services, packages, model assets, user-provided books, or generated media.
