# 自旋电子学文献日报 · Journal-alert

Journal-alert 检索 52 本已启用期刊，通过 OpenAlex 和 Crossref 采集文献，按自旋电子学关键词筛选并去重，生成 Markdown 日报与 GitHub Pages 中英文卡片。**唯一通知渠道为 Bark**。

## 自动运行和推送

- GitHub Actions 工作流 `journal-alert daily` 每天北京时间 07:30 运行，或在 Actions 页面手动运行。
- Bark 每篇论文对应一条通知，包含**中文标题、英文原标题、经过质量检查的中文摘要**以及原文/网页链接。
- 缺少可信英文原文摘要、AI 额度用尽或中文翻译未通过检查的论文**暂不推送**，保存在状态队列中供以后补齐；绝不将英文自动当作已完成中文通知。
- 通知平台有字节数限制：篇幅太长时会明确显示“摘要较长”，附完整页面与原文链接，不能保证一条手机推送容纳完整摘要。
- Bark API 接受请求不等于手机实际接收；程序分别记录每篇论文的服务端确认信息，失败保留重试资格。
- 旧微信/Server酱及 PushPlus 通道和对应发送程序已停止使用并移除。

## GitHub Actions 操作

在 [Actions](https://github.com/cunlongdong/Journal-alert/actions) 中选择 `journal-alert daily` → `Run workflow` → 分支 `main`。

- 默认模式：抓取并去重、生成报告、补齐翻译、发送 Bark。
- `no_push`：抓取并加入待发队列，本次不推送。
- `flush_only`：只尝试补发待处理的论文（同时恢复缺失英文摘要）；不再次抓取期刊。
- `cards_only`：只重建文献网页；不发 Bark。
- `backfill_only`：历史论文补摘要和中文标题，最多处理 `history_max` 指定的论文（上限 15）；**不会向 Bark 补发旧通知**。
- `rebuild_only`：不进入待发队列，但会将新论文标记为已见；谨慎使用。
- `delivery_status`：查看待处理队列、缺少英文摘要数量、Bark 接受确认等。
- `test_api` / `test_bark`：分别检查模型网关或发送一条 Bark 连接测试。

## 必要配置

仓库 **Settings → Secrets and variables → Actions**：

- Secret `BARK_DEVICE_KEY`：Bark 设备密钥。
- Secret `CUSTOM_AI_API_KEY`：翻译网关的 API 密钥。
- Variables `CUSTOM_AI_BASE_URL`、`CUSTOM_AI_MODEL`：翻译服务地址与模型名称。
- Variable `JALERT_BARK_ENABLED`：设置为 `true`；默认已启用。
- Variable `JALERT_AI_MAX_CALLS_DAY`：单日模型请求次数；未设置时默认为 **200**。如果之前显式设置为 `25`，需要自行调整，否则仍只能使用 25 次。增加次数可能增加服务费用或受到提供商限流。
- `JALERT_BARK_BODY_BYTES`：通知正文上限（默认 2400 字节）；`JALERT_BARK_MAX_MESSAGES_RUN`：单轮安全尝试上限。

配置中的 API 密钥不应提交到仓库；设置均通过 GitHub Secrets 读取。

## 中英文完整性

GitHub Actions 的 **中英文标题与摘要完整性（实际日报）** 区块会列出中文标题、原始英文摘要、中文摘要覆盖数量。英文数据源没有提供某些文章的原始 Abstract 时，不允许通过 AI 捏造原文。程序会使用 DOI 查询 OpenAlex、Crossref 和可用的 Semantic Scholar 资料；仍找不到时保留“暂缺”状态。

历史报告及此前已发出的英文 Bark 通知不会自动再次推送，以避免重复通知。后续 `backfill_only` 或每日自动补译会逐步丰富双语网页。旧论文的发送确认记录仍然保留。

网页：[自旋电子学论文卡片墙](https://cunlongdong.github.io/Journal-alert/)。离线测试：[journal-alert offline checks](https://github.com/cunlongdong/Journal-alert/actions/workflows/tests.yml)。

## 本地检查

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python run.py --strict-check
python tools/reliable_push.py --status
python tools/audit_site_fullness.py
```

状态文件位于 `state/`；`seen.sqlite` 是去重历史，`enhanced_wechat_outbox.json` 是沿用旧文件名的持久化论文通知队列（现仅供 Bark 使用），`enhanced_zh_cache.json` 是中译文和中文标题缓存，`dual_channel_delivery.json` 沿用旧文件名存储 Bark 确认。不要直接删除状态文件，否则可能重复推送。旧文件名是为了在升级后保留历史数据兼容性。

关键词和 52 本期刊清单：[`config.json`](config.json)。历史报告：`reports/`。
