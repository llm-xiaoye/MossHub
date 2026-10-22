# 排错

| 现象 | 先检查 |
| --- | --- |
| No module named raghub | 是否使用 RAG_HOME/runtime/python/bin/python；PYTHONPATH 是否是 RAG_HOME/project/backend |
| 缺少 yaml、docling、FlagEmbedding 等 | 是否按 uv.lock 安装了全部需要的 extras；不要临时混用系统 site-packages |
| required directory missing or aliased | 仓库应位于 RAG_HOME/project；先执行 initialize_home.py；RAG_HOME 不经 symlink 别名 |
| 模型/解析锁缺失 | 先执行 prepare_resources.py --download；模板不是已安装锁 |
| 模型大小、SHA 或 package version mismatch | 核对下载完整性和 uv.lock；不跳过验证，不用旧机器的绝对路径锁 |
| Docker CLI unavailable | Docker Desktop 已启动；RAG_DOCKER_BIN 是真实可执行文件的绝对路径 |
| Compose 镜像无法下载 | 网络或镜像源可达性；当前镜像固定 arm64 和摘要，修改镜像需要重新验证 |
| 页面空白或静态文件缺失 | frontend/npm ci 和 npm run build 是否完成；查看实际 API 错误 |
| start 拒绝旧记录或端口占用 | 先 status，核对实例与角色锁；不删除未知状态来强行启动 |
| DeepSeek credential file missing | RAG_HOME/private/deepseek.env 是否存在且含 DEEPSEEK_API_KEY；不要把密钥写入源码 |
| QUERY 没有可问资料 | 知识库是否有成功发布的索引，资料是否待复核或已移除 |
| Obsidian 列表为空 | 本发布版读取 RAG_HOME/obsidian，作者的原笔记未上传 |
| 网页采集缺浏览器 | PLAYWRIGHT_BROWSERS_PATH 与服务一致，并安装 Chromium |
| 资源准备中途失败 | 保留报错和部分文件，核对网络、版本与校验；准备脚本不覆盖现有目标，排查后使用新的干净安装根 |

日志在 RAG_HOME/logs，状态在 RAG_HOME/tmp。日志和数据可能含资料正文、路径或请求信息，提交 issue 前自行脱敏。不要把 API key、完整聊天、数据库、原件或私人笔记附到公开 issue。
