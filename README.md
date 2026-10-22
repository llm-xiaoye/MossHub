# Moss Hub

本机单用户知识库与 RAG 原型：资料获取、解析复核、结构感知切块、索引发布，以及有原文来源的多轮问答。

这个仓库是 **2026-10-22 当前开发工作区的脱敏导出**，包含最新桌面界面和未提交功能；使用独立初始提交，不携带旧 Git 历史。Python 包名和内部命令继续使用 `raghub`。

## 能做什么

- **资料管理**：本地文件、网页、目录及 Obsidian 笔记选择导入；归档、任务进度、解析复核、版本发布与失败恢复。
- **多格式处理**：PDF、Markdown、HTML、DOCX、XLSX、CSV、JSON 等；Docling 与分格式适配，按需 RapidOCR，表格和原文定位。
- **检索与问答**：BGE-M3、Milvus dense + Jieba/BM25、融合、BGE reranker、父块扩展、DeepSeek Flash、多轮记忆与 SSE。
- **来源追溯**：支持单元、原文摘录、页码/行/单元格等定位；检索、证据和记忆详情。
- **生命周期**：IMPORT / QUERY 分时运行、SQLite 任务状态与发件箱、Celery + Redis、版本与移除过滤。

界面为 React / TypeScript / Vite / Ant Design，当前维护重点是电脑端。后端为 FastAPI；SQLite 保存业务状态，Milvus 保存检索数据，本地文件保存原件、解析产物和父块。

## 使用前先了解

原型此前在 **macOS Apple Silicon、Python 3.12** 上使用；Compose 当前固定 `linux/arm64` 镜像。Linux/x86、Windows 没有在这份导出上验证，不能直接视为支持。源代码使用 `fcntl`，Windows 不是直接运行目标。

本次发布的检查见 [交付说明](docs/RELEASE.md)。发布副本没有执行从零下载全部资源、启动完整服务或真实云端问答；下述流程是按当前代码编写的安装说明，首次安装仍需在你的机器上验证。仓库不附带作者资料、聊天记录、数据库、模型权重、运行日志或私有密钥，也不附带可直接浏览的预建知识库。

## 1. 前提条件与目录

需要 Git、uv、Node.js（Vite 7 要求兼容的 Node 版本，推荐使用 Node 24）、npm，以及已启动的 Docker Desktop。模型与容器需要联网下载，磁盘应为依赖、约 5 GB 模型及后续资料留足空间；实际内存和耗时取决于机器，未给出最低性能保证。

当前代码要求固定布局，**不要任意改成仓库目录就是 RAG_HOME**：

```text
MossHub/                    # RAG_HOME：数据与运行根
├── project/                # 本仓库
├── runtime/python/         # 本机安装环境
├── runtime/browsers/       # 网页采集所需 Chromium
├── models/                 # 下载后校验的模型 / tokenizer
├── private/deepseek.env    # 你自行填写的私有密钥
├── obsidian/               # 可选，只读导入的笔记副本
├── data/                   # 新实例数据库、原件和处理结果
├── cache/
├── logs/
└── tmp/
```

```sh
mkdir -p "$HOME/MossHub"
cd "$HOME/MossHub"
export RAG_HOME="$(pwd -P)"
git clone https://github.com/llm-xiaoye/MossHub.git "$RAG_HOME/project"
export PYTHONPATH="$RAG_HOME/project/backend"
export PYTHONDONTWRITEBYTECODE=1
```

该 GitHub 仓库为私有仓库，clone 需要你的 GitHub 登录权限。RAG_HOME 必须是无符号链接别名的绝对路径。已有同名目录或实例时，先检查内容；不要覆盖正在使用的数据。

## 2. 安装 Python 依赖

```sh
UV_PROJECT_ENVIRONMENT="$RAG_HOME/runtime/python" \
  uv sync --project "$RAG_HOME/project" --frozen --all-extras --python 3.12

"$RAG_HOME/runtime/python/bin/python" -B \
  "$RAG_HOME/project/scripts/initialize_home.py"
```

`uv.lock` 固定依赖；`models`、`ingestion`、`evaluation` extras 分别覆盖本地模型、采集解析与离线评分。不要用系统 Python 或另一项目的 venv 代替这个解释器。准备脚本会核对资源锁中要求的精确包版本；版本不符时先处理安装问题，不能忽略校验。

## 3. 配置 DeepSeek 密钥与 Obsidian

```sh
cp "$RAG_HOME/project/configs/examples/deepseek.env.example" \
   "$RAG_HOME/private/deepseek.env"
chmod 600 "$RAG_HOME/private/deepseek.env"
```

用文本编辑器把文件中的占位值换成你的真实 `DEEPSEEK_API_KEY`。不要把密钥提交到 GitHub。后端从该文件读取；只在 shell 中 export 密钥不替代这个文件。当前调用配置是 `deepseek-flash`、`https://api.deepseek.com`，云端操作会产生费用，模型别名及供应商服务以实际返回为准。

可选：把自己的 Markdown 笔记及其本地图片副本放到 `$RAG_HOME/obsidian`，保留相对目录。发布版以这里作为 Obsidian 只读导入根，未包含作者的原库，也不会修改该根中的笔记。若不需要 Obsidian，留空即可。

## 4. 准备模型与浏览器资源

```sh
"$RAG_HOME/runtime/python/bin/python" -B \
  "$RAG_HOME/project/scripts/prepare_resources.py" --download

PLAYWRIGHT_BROWSERS_PATH="$RAG_HOME/runtime/browsers" \
  "$RAG_HOME/runtime/python/bin/python" -B -m playwright install chromium
```

资源脚本按仓库中固定的 revision、成员大小与 SHA-256 下载 BGE-M3、reranker、Docling 模型、RapidOCR 和官方 DeepSeek tokenizer 数据；不执行 tokenizer 压缩包中的 Python 代码。原来含作者绝对路径的两份锁已改为 `configs/templates/` 中的模板，安装时才生成当前机器的实际锁。

这是显式联网准备步骤，正常服务运行使用本地资源。资源下载或校验失败时保留现场，检查报错；脚本不会覆盖已存在的目标。不要用未校验文件冒充安装成功，也不要反复对不明的半成品目录重跑。

模型和上游依赖各自受其许可证约束；此仓库未重新分发它们的权重。

## 5. 构建前端

```sh
cd "$RAG_HOME/project/frontend"
npm ci
npm run build
```

生产文件写入 `frontend/dist`，由 API 提供。node_modules 和 dist 均不入库。开发时可在同目录运行 `npm run dev`；开发网页通常使用 5173，后端仍需就绪。

## 6. 启停和就绪检查

先打开 Docker Desktop。如果 CLI 不在应用的标准目录，设置它的实际绝对路径：

```sh
export RAG_DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin/docker"
export DOCKER_CONFIG="$HOME/.docker"

"$RAG_HOME/runtime/python/bin/python" -B "$RAG_HOME/project/scripts/ragctl.py" status
"$RAG_HOME/runtime/python/bin/python" -B "$RAG_HOME/project/scripts/ragctl.py" start
```

`start` 协调 Compose、模型服务、API、两个 Celery Worker 和派发器。已有启动记录、角色锁或端口被占用时先核对状态，不能把重复 start 当作恢复手段。

- 资料管理：<http://127.0.0.1:8000/#/documents>
- 知识问答：<http://127.0.0.1:8000/#/chat>
- 就绪：<http://127.0.0.1:8000/health/ready>，应为 HTTP 200 且 `ready=true`
- 存活：<http://127.0.0.1:8000/health/live>，只说明 API 存活

```sh
"$RAG_HOME/runtime/python/bin/python" -B "$RAG_HOME/project/scripts/ragctl.py" stop
```

固定端口包含 8000、8001、19530、6379；多个实例应串行使用。不要为了发布代码停止其他正在使用的实例。Compose 的 MinIO 使用公开的本地开发默认凭据，只用于本机隔离开发；API、Redis、Milvus 的暴露端口绑定回环地址。这不是多用户生产部署方案。

## 7. 从零使用

1. 打开资料管理，新建/选择知识库，切换到 IMPORT。
2. 导入少量自己有权使用的资料，例如 [最小示例](examples/getting-started.md)。确认批次、文件与知识库后受理。
3. 等待归档、解析和质量检查；待复核时先对照原件修订，接收完成不等于解析完成。
4. 发布可用文档版本，构建并发布索引；界面中的失败原因需要先处理。
5. 切换 QUERY，打开知识问答并创建绑定当前知识库的会话。
6. 提问“这个示例项目叫什么？”，核对答案、引用原文和检索详情，再进行追问。
7. 更新或移除资料后，按界面的版本和索引流程操作；旧会话受代次只读规则约束。

这只是操作示例，仓库未预生成其解析结果或答案。目录导入、Obsidian、原文复核与恢复的具体边界见 [使用说明](docs/USAGE.md)。

## 开发检查

```sh
cd "$RAG_HOME/project"
"$RAG_HOME/runtime/python/bin/python" -B -m unittest discover \
  -s tests -p 'test_question_bubble_contract.py'
cd frontend
npm run build
```

`tests/` 中还包含需真实模型、服务或其他已准备数据的集成检查；不要把全部 discover 当作无外部依赖的单元测试。两份依赖作者外部技能/历史备份的测试没有附带。检查执行结果与范围见 [交付说明](docs/RELEASE.md)。

## 排错与开发入口

参见 [排错](docs/TROUBLESHOOTING.md)、[架构与目录](docs/ARCHITECTURE.md)、[脱敏边界](docs/RELEASE.md)。

不把少量演示题、历史局部回归或引用存在当成整体正确率、安全证明或生产就绪声明。
