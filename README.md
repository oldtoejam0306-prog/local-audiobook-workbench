# 本地有声书工作台（试验版）

Windows 工作台调用 DeepSeek 分析选定章节，在 Apple Silicon Mac 上通过 Huashuo 和 Qwen3-TTS 生成语音。人物档案、声音绑定、纠错说明、任务状态与费用回执按书保存。

## 功能

- 按章节范围分析人物，保留原文引文、来源哈希、别名和不确定性。画像任务按至多三章分批推进。
- 根据人物画像生成虚构声音描述，经 VoiceDesign 与 speaker encoder 形成专属声音向量。工作台支持编辑描述、生成候选、直接试听和显式采用。
- 固定旁白与人物声线，画像更新保存新的创作候选，已选声线独立保留。
- DeepSeek 负责对白归属与章节摘要，本地 Qwen3-TTS 负责语音。无法逐字核对的归属依据会单独降级并记录审计。
- 单进程多角色整章渲染，合并相邻同角色分段，按标点与话轮控制停顿，保留正文与声音的逐字覆盖证据。
- 章节任务、跨章记忆、来源绑定的纠错说明、回执与音频哈希核验、直接播放和保音高倍速。
- 相同分析输入复用已有有效回复，本地合成复用完成的语音单元。任务支持在当前阶段结束后暂停。
- 费用窗口汇总实际 provider token usage，跨目录按响应身份去重，并区分已知用量、未知状态与冲突。

## 验证范围

在一台 M2 / 16GB Mac 与 Windows 控制端上，两章中文素材完成了真实 DeepSeek 分析、人物/声音绑定和本地配音链。原始制作耗时约 25 分钟，产生约 29 分 26 秒音频，使用 10,275 输入和 4,324 输出 tokens。按输入 2 元、输出 8 元每百万 tokens 估算为 0.055142 元。费用估算可在界面调整，实际扣款以供应商账单为准。

整章复验使用已有分析回复，新增 API 请求为 0。最后一次语音复验仍有一条极短对白拖长告警。人物归属中也保留了不确定项及引文核验降级项。整本自动生成、人物语义正确率和全篇听感仍待验证。

公开配置适配器另外完成了 Mac 三声线短文本实机合成与缓存复用，第二次运行 3 个单元全部命中缓存。Mac 上 11 项资源/COW 测试通过。

- Python 离线回归为 427 通过、5 跳过。跳过项涉及 Windows 符号链接/硬链接权限，合成 DOCX 拒绝测试产生一条重复 ZIP 条目提示。
- Node 两组测试共 15 项通过。169 个 Python 文件与 7 个 MJS 文件通过语法检查。

## 环境与配置

Windows 需要 Python 3.10+、Tk、winsound、OpenSSH，以及用于倍速播放的 FFmpeg。Mac 需要已安装的 Huashuo、MLX/MLX Audio、NumPy、兼容的 Qwen3-TTS 模型、FFmpeg、FFprobe 与 macOS sandbox-exec。Node.js 18+ 用于 DeepSeek worker。

仓库分发源码、测试、占位配置和许可证。权重、书稿、索引、声线向量、音频、调用回执与填写后的配置由用户保存在私有目录。

1. 将源码放到 Windows 和 Mac，在各自私有目录填写 `config.example.windows.json` 与 `config.example.mac.json` 的副本。
2. 配置 SSH 主机别名、专用 known_hosts、远端 Python 和源码/数据根目录。连接保持主机密钥核验。
3. 分别登记 VoiceDesign、speaker encoder 与 CustomVoice 本地模型路径和 inventory。CustomVoice 路径指向完整 HF 缓存根，其中包含 `hub/models--*/snapshots/*`。
4. 配置 FFmpeg 与 FFprobe。两者可位于不同目录，启动模型前会核验工具可用性。环境变量 `AUDIOBOOK_FFMPEG`、`AUDIOBOOK_FFPROBE` 可覆盖配置。
5. 配置受信的书籍索引注册表。Windows 的 `workbench.books` 登记显示名、book_id 和 chapters 列表。Mac 的 `mac.index_registry` 指向由 `verified_index_rpc` 读取的注册表。默认书目为空。
6. 旧任务工作区通过 `remote.legacy_chapter_root` 登记。全新专用环境可显式选择 `remote.legacy_mode=none` 并设置 `workbench_v2.single_user_ack=true`。

Windows 启动示例使用本机私有配置。分析需要可用的模型 key，语音生成使用 Mac 本地模型。

```powershell
$env:AUDIOBOOK_CONFIG_FILE = "C:/private/audiobook-windows.json"
$env:DEEPSEEK_API_KEY = "<your-key>"
python workbench_real_v2_ui.py
```

模型 key 只从进程环境读取。工作台会在创建调用任务前做凭据预检，结果不确定的付费请求会停下并保留原始回执。

## 准备新的书籍

目前 UI 使用已登记的书籍。新书通过准备、Huashuo 导入、章节核对、索引构建与注册接入。TXT/DOCX 可进入当前 verified-index 流程，EPUB 需先整理为可验证的 TXT/DOCX。

```sh
python book_import.py prepare /absolute/path/to/book.txt
```

该命令保存隔离副本、来源哈希与章节候选。然后使用已安装 Huashuo 的 import/check 命令建立并核对 `<book_root>/work.huashuo`。`python book_import.py repair-chapters <workdir>` 可对符合前置条件的工作区进行带备份的章节修复。

核对后可通过 Python API 构建外置索引。`book_root` 与 `expected_book_id` 使用 prepare 输出，索引目录选在书籍工作区之外。

```python
from verified_workspace_index import build_verified_workspace_index

result = build_verified_workspace_index(
    book_root="/private/books/book-root",
    workdir="/private/books/book-root/work.huashuo",
    expected_book_id="book-0123456789abcdefabcd",
    experimental_workspace="/private/indexes/new-book-index",
)
```

将 `result["index_workspace"]`、`result["snapshot"]` 和 `result["index_sha256"]` 连同 book_root/workdir 登记到 Mac 的受信注册表，格式见 `verified_index_rpc.load_registry`。注册表采用 `version=1` 与 `books` 字典，Windows 同时登记对应 book_id/display_name。索引、章节边界与材料处理权限由操作者核对。

## 日常操作

读取书籍索引，选择章节范围。可先分析范围内的人物档案，再开始分析与配音。声音页可编辑创作描述、试听候选并采用。查看已有章节进度和播放只读取已有回执。纠错说明保存独立历史，后续分析把其作为操作者说明引用。

Mac 模型任务使用串行资源锁、入口内存/磁盘门槛、运行监控与任务级 caffeinate。模型缓存使用 APFS COW，完整语音单元按文本、声线、引擎与来源参数复用。掉电和系统重启后的完整恢复列在后续验收范围。

## 许可与资料

源码按 Apache-2.0 提供，第三方依赖及模型使用各自许可证。具体范围与旧协议迁移记录见 [MATERIALS.md](MATERIALS.md)，分发文件见 [SOURCE_ALLOWLIST.md](SOURCE_ALLOWLIST.md)，逐文件摘要见 [CLEAN_COPY_MANIFEST.json](CLEAN_COPY_MANIFEST.json)。

本地 TTS 使用官方 Huashuo API，人物与记忆管理采用本项目的独立适配层。上游项目与研究参考如下。

- [wixette/huashuo](https://github.com/wixette/huashuo)
- [gwaves/novel_reader](https://github.com/gwaves/novel_reader)
