# 本地有声书工作台（试验版）

Windows 工作台调用 DeepSeek 分析选定章节，在 Apple Silicon Mac 上通过 Huashuo 和 Qwen3-TTS 生成语音。人物档案、声音绑定、纠错说明、任务状态与费用回执按书保存。

## 项目定位与制作流程

工作台面向需要把已整理并登记的中文书稿制作成多角色有声章节的使用者。它把章节材料、人物档案、创作声音和章节音频按书管理。分析按选定范围分批进行，配音可以逐章完成，使用者可在试听后修订段落。

```mermaid
flowchart LR
    A[登记书籍与章节索引] --> B[选择章节范围]
    B --> C[分析人物档案与跨章记忆]
    C --> D[检查并采用本书角色声线]
    D --> E[DeepSeek分析对白归属与摘要]
    E --> F[Mac本地Qwen3-TTS逐章合成]
    F --> G[播放、抽听与段落修订]
```

每本书保存独立的人物档案和角色声音绑定。VoiceDesign 根据人物档案与创作描述设计虚构声线，speaker encoder 将设计音频编码为该角色专属的声音向量。不同书籍可为同名角色保留独立声线。合成阶段使用共享的本地 Qwen3-TTS 模型，向量提供每本书、每个角色的声音条件。

DeepSeek 对选定章节输出结构化对白归属、朗读情绪和章节摘要。程序按照固定的角色声音表，为每段文字选取对应向量，再把文字和朗读指令交给 TTS。Huashuo 负责语音计划、单元缓存、停顿排版与章节打包。逐字依据不足的归属会保留降级状态与审计信息。

## 人物档案与跨章记忆

- 人物档案包含姓名、别名、身份、性别、年龄、性格、行为、说话习惯和虚构声音设计。字段保留来源章节、原文引文及明确事实、推断或未知标记。
- 声音设计记录音高倾向、音色、咬字、节奏与情绪基线，使用者通过试听选择适合的候选。
- 后续章节引用经过压缩的人物索引、章节摘要和纠错说明，压缩过程保留省略统计。
- 发现缺档人物时可以定向补充画像。关系与别名的证据有歧义时，保存待核信息。
- 已采用的声线形成按书保存的绑定。采用新候选后，新的制作任务按该绑定生成；已有章节可以重新制作。
- 任务回执记录原文、人物映射、声音向量与音频哈希，便于核对历史产物。复杂历史记忆冲突通过显式版本选择 API 处理。

## 工作台页面与可调整内容

- **章节** 页面读取已登记索引并选择章节范围，可分析所选章节人物、开始分析与配音、请求当前阶段完成后暂停、查看人物归属原因、查看章节进度和播放已有音频。暂停会等待当前分析或合成阶段结束。
- **校听** 页面打开章节段落修订，查看经回执核验的原文与声音信息，可修改朗读指令或选择该章已有绑定中的说话人，再生成候选、试听、采用或恢复原章。
- **人物与声音** 页面查看按书保存的人物档案、来源引文与不确定项，填写供后续章节引用的纠错说明，也可查看旁白和人物绑定、编辑虚构声音描述、生成声音候选、试听并明确采用。画像更新会形成新的声音创作候选。

声音描述影响 VoiceDesign 所生成的候选声线。例如可以描述为“音区明亮偏高、吐字灵活、节奏轻快，适合话多的喜剧角色”。段落修订中的朗读指令作用于所选段落的候选合成，例如“少拖腔，句尾自然收住”。效果通过候选试听判断。

播放倍速控制收听速度并保持音高，范围为 0.8 至 1.5 倍。生成段落候选后可以试听、采用或恢复原章播放。修订记录包含原文、声线和音频哈希，整章重排会显示实际生成与缓存复用数量。

章节分析可使用跨章记忆和来源绑定的纠错说明。相同分析输入会复用有效回复，语音单元则按文本、声线、引擎和来源参数复用已完成缓存。界面显示实际新生成和缓存复用计数。任务可在当前阶段完成后暂停，之后从已保存的阶段与缓存继续处理。新书需先经过准备、章节核对、索引构建和注册，当前界面列出的是已登记书籍。

## 使用者听感与交互验收

可以选择已有完成音频的章节进行体验验收，重点检查以下内容。

- 普通旁白是否自然，长句、短句和句尾是否顺畅。
- 不同角色是否容易区分，旁白与对白交接是否自然。
- 是否有漏字、多字、句尾拖长、笑声或多余语气声。
- 播放、停止和 0.8 至 1.5 倍速控制是否符合预期。
- 段落修订是否能生成候选、试听、采用，并在需要时恢复原章。

ASR 抽检通过命令行执行，输出部分语音单元的转写差异候选，供听音核对。当前端到端实测覆盖连续八章，全篇人物语义、主观听感和长期无人值守能力保留为待验证项。

反馈时写上章节、播放时间和听到的问题，便于定位需要修订的段落。

当前待完善的使用入口包括新书准备与登记的图形界面，以及复杂历史记忆冲突的图形化版本选择。现有新书接入流程见下方准备说明，历史版本选择由对应 API 提供。

## 功能

- 按章节范围分析人物，保留原文引文、来源哈希、别名和不确定性。画像任务按至多三章分批推进。
- 根据人物画像生成虚构声音描述，经 VoiceDesign 与 speaker encoder 形成专属声音向量。工作台支持编辑描述、生成候选、直接试听和显式采用。
- 固定旁白与人物声线，画像更新保存新的创作候选，已选声线独立保留。
- DeepSeek 负责对白归属与章节摘要，本地 Qwen3-TTS 负责语音。无法逐字核对的归属依据会单独降级并记录审计。
- 单进程多角色整章渲染，合并相邻同角色分段，按标点与话轮控制停顿，保留正文与声音的逐字覆盖证据。
- 旁白与角色交接采用0.50秒最小停顿，冒号/逗号承接采用0.35秒。规则按旁白边界应用，显式停顿覆盖和已有更长停顿优先。
- 支持闭集情绪提示和旁白自然收句指令。用户可在段落修订窗口填写朗读指令，生成候选、试听、显式采用或恢复原章。
- 段落修订保留不可变候选和原章回执，复用通过校验的缓存。整章重排会显示实际新生成与复用计数。
- 章节任务、跨章记忆、来源绑定的纠错说明、回执与音频哈希核验、直接播放和保音高倍速。
- 相同分析输入复用已有有效回复，本地合成复用完成的语音单元。任务支持在当前阶段结束后暂停。
- 章节出现缺档人物时按目标姓名补充画像。可选别名的证据不足会保留待核审计。
- 新任务失败后仍可核验和播放同索引、同原文的历史完成音频，并明确标记历史版本。
- 声音生成的远端完成回执在文件传输前保存，传输异常保留诊断，支持显式补传已完成的音频与向量。
- 费用窗口汇总实际 provider token usage，跨目录按响应身份去重，并区分已知用量、未知状态与冲突。

## 验证范围

在一台 M2 / 16GB Mac 与 Windows 控制端上，两章中文素材完成了真实 DeepSeek 分析、人物/声音绑定和本地配音链。原始制作耗时约 25 分钟，产生约 29 分 26 秒音频，使用 10,275 输入和 4,324 输出 tokens。按输入 2 元、输出 8 元每百万 tokens 估算为 0.055142 元。费用估算可在界面调整，实际扣款以供应商账单为准。

整章复验使用已有分析回复，新增 API 请求为 0。最后一次语音复验仍有一条极短对白拖长告警。人物归属中也保留了不确定项及引文核验降级项。整本自动生成、人物语义正确率和全篇听感仍待验证。

公开配置适配器另外完成了 Mac 三声线短文本实机合成与缓存复用，第二次运行 3 个单元全部命中缓存。Mac 上 11 项资源/COW 测试通过。

- Python 离线回归为 567 通过、5 跳过。跳过项涉及平台与链接能力，合成 DOCX 拒绝测试产生一条重复 ZIP 条目提示。Node 四组测试共 40 项通过。189 个 Python 文件与 10 个 MJS 文件通过语法检查。

此前的浏览器接线使用自写正文与 PCM 音频完成生成候选、开始试听、采用、恢复原章、诊断和费用操作的离线闭环。已有真实材料完成索引、八章进度、人物档案、章节音频播放、拖动定位、停止重播与 1.08 倍速核验。浅暗主题和菜单的 Enter、Escape、方向键操作在 Chromium 中通过，速度与说话人选择器均实测为 14px 字号、40px 高和 12px 圆角。本轮新增语音修订的实际合成听感留待使用者试听。

实窗验收确认独立 `AudiobookWorkbench.exe` 窗口及图标正常显示，并读取现有服务的八章状态和 16 个人物声音档案。C005 音频总时长约 10 分 20 秒，抽测播放进度增长至 28 秒并停止成功。费用页读取 30 条 confirmed、1 条 unknown，共 31 条回执。重复启动前置现有窗口，进程中只有一个 host。关闭窗口后原服务仍运行。另在隔离空书目中验证了页面“退出工作台”会关闭 host 和隔离服务。本次未发起新的语音合成或付费生成。

段落修订已完成真实工作台操作验收，包含生成候选、启动试听、采用、普通章节播放和恢复原章。该次整章重排新生成2个单元、复用80个单元。另有两章各8个语音单元通过本地ASR抽样转写对照，原文和音频哈希保留在私有验收记录中。

2026-11-11完成了连续八章范围，产生82分31秒音频。最后一次执行49分30秒，复用了此前完成的语音缓存。有新语音生成的任务监控在有限试验中累计66分8秒，峰值采样RSS为2.86GiB，最低空闲内存比例33%。13个旁白或角色绑定通过跨章一致性校验。

这八章各抽检8个语音单元，共64个，发现5项转写差异候选，涉及句尾、笑声、同音字和额外语气声，保留听音确认。另有两项时长提示待使用者听音判断，原文结巴句的合成时长偏长，长旁白的合成时长偏快。八章归属与制作目录五笔画像回执合计90,650 tokens，按输入2元、输出8元每百万估算0.321754元。原始回执与音频保存在私有目录。

新书需要按下方步骤准备并登记。复杂历史记忆冲突提供显式版本选择API，界面入口仍待完善。

## 环境与配置

Windows 默认桌面入口需要 Python 3.10+（包含 Tk 与 `winsound`）、.NET Framework 4.6.2+、已安装的 Microsoft Edge WebView2 Runtime、OpenSSH 和用于倍速播放的 FFmpeg。Mac 需要已安装的 Huashuo、MLX/MLX Audio、NumPy、兼容的 Qwen3-TTS 模型、FFmpeg、FFprobe 与 macOS sandbox-exec。Node.js 18+ 用于 DeepSeek worker。

仓库分发源码、测试、占位配置和许可证。权重、书稿、索引、声线向量、音频、调用回执与填写后的配置由用户保存在私有目录。

1. 将源码放到 Windows 和 Mac，在各自私有目录填写 `config.example.windows.json` 与 `config.example.mac.json` 的副本。
2. 配置 SSH 主机别名、专用 known_hosts、远端 Python 和源码/数据根目录。连接保持主机密钥核验。
3. 分别登记 VoiceDesign、speaker encoder 与 CustomVoice 本地模型路径和 inventory。CustomVoice 路径指向完整 HF 缓存根，其中包含 `hub/models--*/snapshots/*`。
4. 配置 FFmpeg 与 FFprobe。两者可位于不同目录，启动模型前会核验工具可用性。环境变量 `AUDIOBOOK_FFMPEG`、`AUDIOBOOK_FFPROBE` 可覆盖配置。
5. 配置受信的书籍索引注册表。Windows 的 `workbench.books` 登记显示名、book_id 和 chapters 列表。Mac 的 `mac.index_registry` 指向由 `verified_index_rpc` 读取的注册表。默认书目为空。
6. 旧任务工作区通过 `remote.legacy_chapter_root` 登记。全新专用环境可显式选择 `remote.legacy_mode=none` 并设置 `workbench_v2.single_user_ack=true`。

Windows 启动示例使用本机私有配置。分析需要可用的模型 key，语音生成使用 Mac 本地模型。

Windows 倍速播放依次查找 `AudioPlayback(ffmpeg=...)` 显式参数、`AUDIOBOOK_PLAYBACK_FFMPEG`、`AUDIOBOOK_FFMPEG`、PATH 中的 `ffmpeg`，最后查找 `%LOCALAPPDATA%/AudiobookTrials/tools/ffmpeg/ffmpeg.exe`。显式参数或环境变量指向无效文件时会提示错误。`AUDIOBOOK_PLAYBACK_FFMPEG` 可单独指定 Windows 播放工具，`AUDIOBOOK_FFMPEG` 也用于 Mac 生成端的工具配置。两台机器分别设置各自的本机路径。

```powershell
$env:AUDIOBOOK_CONFIG_FILE = "C:/private/audiobook-windows.json"
$env:AUDIOBOOK_WORKBENCH_ROOT = "C:/private/audiobook-workbench"
$env:DEEPSEEK_API_KEY = "<your-key>"
python workbench_desktop.py
```

`workbench_desktop.py` 默认寻找 `desktop/bin/AudiobookWorkbench.exe`，也可用 `AUDIOBOOK_DESKTOP_HOST` 指定 host 路径。图标默认使用 `assets/app-icon/audiobook-workbench.ico`，可由 `AUDIOBOOK_APP_ICON` 覆盖。源码分发不包含 EXE、WebView2 SDK DLL 或 loader DLL。首次使用需通过本机已安装的 .NET Framework C# 编译器构建 host：

```powershell
.\desktop\build_desktop.ps1 -SdkDir "C:\path\to\Microsoft.Web.WebView2.SDK" `
  -OutputDirectory ".\desktop\bin" `
  -IconPath ".\assets\app-icon\audiobook-workbench.ico"
```

`-SdkDir` 指向 WebView2 SDK 目录，其中 Core/WinForms DLL 可直接位于该目录，或位于 `lib\net462` 子目录。`runtimes\win-x64\native\WebView2Loader.dll` 应位于该目录下。脚本使用 `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe` 编译 x64 WinForms host，并把所需 WebView2 DLL、loader、图标和许可文本复制到输出目录。构建产物只供本机运行，不属于源码分发。

桌面 host 使用 WebView2 显示工作台页面，通过 `workbench_web.py --no-browser` 复用或启动本机 Python service。首次构建后运行 `python workbench_desktop.py`。如果 host 或 WebView2 Runtime 缺失，会显示错误，不会自动打开系统浏览器。重复启动会前置现有桌面窗口。关闭窗口只关闭 host，后台 service 与进行中的任务继续运行，重新打开可查看任务状态。页面中的“退出工作台”仅在空闲时关闭本机 service。旧 Tk 入口仍可由使用者显式运行 `python workbench_real_v2_ui.py`。浏览器模式也可显式运行 `python workbench_web.py`，都不是桌面入口的自动 fallback。

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

选择已有完成音频的章节，点击“修订段落音频”，选取段落并填写朗读指令。生成后先试听，再决定采用或恢复原章。缓存复用取决于语音单元、声线与生成参数是否一致，界面会显示本次实际计数。

浏览器界面通过章节工作台、校听编辑台和人物配音台连接同一套制作适配层。校听页展示经回执核验的原文段落，可修改所选段落的朗读指令，或从该章已有绑定中选择说话人。修订会生成完整章节候选，播放器支持真实播放进度、拖动定位、停止和倍速。候选需先开始试听，再显式采用。人物页可查看身份、别名、事实状态、来源引文和待核信息，并保存与章节来源绑定的纠错说明。

每章选用一个基于原章的修订版本，段落修订沿用该章冻结的角色声线。采用新的书籍声线后，从章节页重新配音可将新绑定用于新的章节版本。历史成品会在列表中标明。人物页的纠错说明绑定当前校听章节的来源。

声音页在当前窗口内按书籍与人物保存描述草稿、候选和试听状态。切换页面后重新读取相同档案时可继续编辑与采用。人物方案或选定声线更新后，相关候选会重新校验。人物详情的长文本支持独立滚动和窗口缩放。

费用窗口的“刷新回执”在后台扫描本机记录，“按新单价重算”使用当前扫描结果重新估算。重复刷新合并为后续扫描，扫描失败时保留上次结果。

同一音频和倍速的播放副本通过哈希、PCM 格式与时长核验后复用。停止播放后副本留在本机缓存，缓存按最近使用情况清理，保留正在准备或播放的文件。开始播放前再次核对源音频与副本的哈希。

Mac 模型任务使用串行资源锁、入口内存/磁盘门槛、运行监控与任务级 caffeinate。模型缓存使用 APFS COW，完整语音单元按文本、声线、引擎与来源参数复用。掉电和系统重启后的完整恢复列在后续验收范围。

## 许可与资料

源码按 Apache-2.0 提供，第三方依赖及模型使用各自许可证。具体范围与旧协议迁移记录见 [MATERIALS.md](MATERIALS.md)，分发文件见 [SOURCE_ALLOWLIST.md](SOURCE_ALLOWLIST.md)，逐文件摘要见 [CLEAN_COPY_MANIFEST.json](CLEAN_COPY_MANIFEST.json)。

本地 TTS 使用官方 Huashuo API，人物与记忆管理采用本项目的独立适配层。上游项目与研究参考如下。

- [wixette/huashuo](https://github.com/wixette/huashuo)
- [gwaves/novel_reader](https://github.com/gwaves/novel_reader)
