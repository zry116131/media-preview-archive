# 素材预览 · AI NAS 素材工作台

本仓库为公开、只读的脱敏源码归档，公开前复查记录见 [ARCHIVE.md](ARCHIVE.md)。下文的部署与功能验证描述保留自历史开发记录；不代表当前已上线或已重新完成真实 NAS / AI 服务验收。应用没有登录鉴权，只适合可信内网使用。

这是设计方案的第一阶段可运行 MVP，当前包含：

- 用户侧统一使用 Windows UNC 项目地址，后台转换为 SMB 地址并解析应用计算节点内部挂载路径
- 后台增量扫描与任务状态
- 素材快速内容指纹
- 重命名/移动识别
- SQLite 素材索引
- 图片/视频通过 NAS File Station 临时地址从 NAS 存储节点直连预览（不经应用计算节点转发）
- 扫描后自动排队生成和缓存预览图，失败素材可重试
- 素材网格、筛选、详情抽屉
- 分页增量加载，支持超过 500 条素材的大项目
- 按项目内一级文件夹分组展示素材
- SMB 挂载与 NAS 统一账户健康检查
- JoyAI 按素材规格自适应分流、范围选择、暂停与重启断点续跑
- 文案分镜工作台、版本管理、导入导出和分镜册安全删除
- 扫描与全量分析完成后才解锁分镜创作；无法识别的异常素材可先单条重试，确认无效后再记录原因并人工排除
- 人工排除不删除 NAS 文件，可随时恢复；排除与内容指纹绑定，文件内容变化后下次扫描自动回到待分析
- 未就绪时历史分镜保持只读，但仍可预览、导出或删除整册
- JoyAI 视觉模型与 DeepSeek/本地文本模型状态管理

当前 JoyAI-VL-Interaction Preview · 8B 级已通过本地 vLLM 部署，扫描和 AI 分析均由用户手动触发。

视频分析会根据时长自动选择整段或分段原生视频理解：30 秒以内整段 2 FPS，30 秒到 2 分钟整段限帧，2 到 10 分钟按 60 秒切片，10 分钟以上按 90 秒切片。4K 与高帧率源只在应用计算节点生成临时 512 像素分析片段；任务完成后删除，不修改 NAS 原文件。某一视频片段原生理解失败时，仅该片段降级为 4 帧兼容分析。

## 脱敏部署示例

- 应用计算节点：承载网页、扫描、预览生成与 AI 服务，示例地址 `192.0.2.10`
- NAS 存储节点：承载原始素材与 File Station，示例地址 `192.0.2.20`
- 局域网入口：<http://192.0.2.10:18080>
- 应用目录：`/srv/media-workbench/app`
- 数据目录：`/srv/media-workbench/data`
- 用户服务：`media-workbench.service`
- SMB 固定挂载：`/mnt/content`（只读）
- SMB 开机挂载：`mnt-content.automount`
- 演示项目：`/srv/media-workbench/demo`

`192.0.2.0/24` 是 RFC 5737 文档示例网段。以上地址、目录和设备名称均为归档占位信息，与实际测试网络和设备无对应关系。当前入口没有登录鉴权，只适合可信内网使用，不应映射到公网。

## JoyAI 局域网 API

- OpenAI 兼容 Base URL：`http://192.0.2.10:7060/v1`
- 模型：`JoyAI-VL-Interaction-Preview`
- 认证：`Authorization: Bearer <API_KEY>`

局域网入口由 `joyai-lan-proxy.service` 提供，只绑定应用计算节点的 `192.0.2.10` 网卡地址。原 vLLM 仍仅监听 `127.0.0.1:7060`；API Key 保存在远端 `~/.config/joyai-lan-proxy.env`，不进入仓库。

调用示例：

```bash
curl http://192.0.2.10:7060/v1/chat/completions \
  -H 'Authorization: Bearer <API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"JoyAI-VL-Interaction-Preview","messages":[{"role":"user","content":"你好"}]}'
```

## 本地运行

```bash
python3 app/server.py --host 127.0.0.1 --port 18080
```

浏览器访问：<http://127.0.0.1:18080>

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 环境变量

- `MEDIA_APP_DATA_DIR`：SQLite 和缩略图缓存目录，默认 `./data`
- `MEDIA_APP_ALLOWED_ROOTS`：允许解析到的内部挂载根目录，多个目录用 `:` 分隔；为空时不额外限制
- `FFMPEG_BIN`：FFmpeg 命令，默认 `ffmpeg`
- `FFPROBE_BIN`：FFprobe 命令，默认 `ffprobe`
- `ANALYSIS_TEMP_DIR`：原生视频理解的临时片段目录，必须同时加入 vLLM 的 `--allowed-local-media-path`
- `FFMPEG_VAAPI_DEVICE`：VAAPI 渲染设备；配置后优先硬件生成临时分析片段，失败自动回退软件编码
- `NAS_BASE_URL`：应用计算节点访问 NAS 存储节点的 HTTPS 地址，例如 `https://192.0.2.20:5001`
- `NAS_PUBLIC_BASE_URL`：浏览器直连 NAS 存储节点的地址，默认与 `NAS_BASE_URL` 相同
- `NAS_ACCOUNT` / `NAS_PASSWORD`：只读 NAS 统一访问账户；应通过权限为 `600` 的独立环境文件注入
- `NAS_VERIFY_TLS`：是否验证 NAS 存储节点 HTTPS 证书；内部节点使用自签名证书时可设为 `false`
- `NAS_SESSION_TTL_SECONDS`：后台复用 File Station 会话的秒数，默认 `2700`
- `MEDIA_APP_SMB_SERVER`：系统级 SMB 挂载对应的服务器，示例为 `192.0.2.20`
- `MEDIA_APP_SMB_SHARE`：系统级 SMB 挂载对应的共享，示例为 `素材库`
- `MEDIA_APP_SMB_MOUNT_ROOT`：稳定的本地挂载根目录，当前为 `/mnt/content`

## Docker

```bash
docker compose up -d --build
```

默认端口为 `18080`。把 NAS 挂载目录添加到 `compose.yaml` 的 `volumes` 后，再在网页中创建项目。

## 大项目工作流

1. 摄影完成删废片、重命名和归档。
2. 在网页中填写 Windows UNC 项目地址并手动刷新素材。
3. 扫描完成后系统自动创建单线程预览图缓存任务。
4. JoyAI 分析可按状态、素材类型、项目内文件夹、时长和批次数量选择范围。
5. 系统读取时长、分辨率、帧率和编码格式，自动选择单图、整段视频、60 秒或 90 秒分段策略。
6. 原生视频输入失败时只降级当前片段；详情页显示实际策略、片段数和降级段数。
7. 分析任务会持久化本批素材 ID 和完成位置，暂停或服务重启后继续剩余素材。
8. 分析失败的素材保持门槛关闭：可重试单条或失败批次；确认文件损坏、占位或本项目不使用时，才在详情中人工排除。
9. 当前项目的“已分析 + 已人工排除”覆盖全部有效素材，且至少有一条真实分析结果后，分镜工作区自动解锁。
10. 排除素材不进入 AI 分析或分镜候选；重命名保留排除，内容被修复或替换后扫描会自动撤销排除。
11. 无用分镜册可在“分镜册管理”中永久删除；只清理分镜、历史版本与选择记录，不触碰 NAS 原素材。

素材库每次加载 120 条，点击“加载更多素材”继续获取；后端不再受固定 500 条展示上限影响。

## 健康检查

- 基础服务：`GET /api/health`
- SMB 与 NAS：`GET /api/storage/health?project_id=<项目ID>`
- AI 服务：`GET /api/ai/status`
- 分镜就绪状态：`GET /api/projects/<项目ID>/storyboard-readiness`
- 单条重试异常素材：`POST /api/assets/<素材ID>/analysis/retry`
- 排除或恢复异常素材：`POST /api/assets/<素材ID>/analysis-exemption`
- 删除分镜册：`DELETE /api/storyboards/<分镜册ID>`（请求体需要当前册名确认）

## 应用计算节点服务

仓库中的 `deploy/media-workbench.service` 使用系统 Python 和 FFmpeg，无需额外安装 Python 包。更新代码后同步到应用目录，再执行：

```bash
systemctl --user daemon-reload
systemctl --user restart media-workbench.service
systemctl --user status media-workbench.service
```

## NAS 存储节点 SMB 自动挂载

`素材库` 共享使用 `mnt-content.automount` 按需挂载到 `/mnt/content`，实际 CIFS 参数由 `mnt-content.mount` 管理。挂载为只读，凭据保存在 root 专用的 `/etc/media-workbench/smb-credentials`，权限为 `0600`。

```bash
sudo systemctl status mnt-content.automount mnt-content.mount
findmnt -T /mnt/content
```

机器重启或 SMB 连接断开后，下一次访问 `/mnt/content` 会自动重新挂载；不再依赖登录桌面后才存在的 GVFS 路径。
