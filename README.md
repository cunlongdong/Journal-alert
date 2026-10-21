# 自旋电子学文献日报 · Journal-alert

Journal-alert 检索 52 本已启用期刊，通过 OpenAlex 和 Crossref 采集文献，按自旋电子学关键词筛选并去重，生成 Markdown 日报与 GitHub Pages 中英文卡片。**唯一通知渠道为 Bark**。

## #47 Git 提交冲突与 Bark 确认恢复（2026-10-21）

[日常运行 #47](https://github.com/cunlongdong/Journal-alert/actions/runs/39617398380) 曾在 Bark API 接受 33 篇新文章后，因远程 main 更新导致 git push 失败。通过 [工作流诊断工件 logs-47](https://github.com/cunlongdong/Journal-alert/actions/runs/39617398380/artifacts) 恢复了当时的原始匹配记录与 10 篇待处理 UID，核对出新发现的 38 篇中**33 篇 API 已接受、5 篇需补译**。逐个核对的 33 个 DOI 记录在 [state/bark_run47_accepted.json](state/bark_run47_accepted.json)。

以后重新抓取这 33 个 UID 时，程序会自动把它们标记为 Bark 已确认，避免重复发送。原有 5 篇待补译不会错误地被标记为已发送。此恢复基于 GitHub Actions 的**API 接受结果**，不等同于手机最终送达保证。

新工作流会在处理开始前检查分支是否过期；发生失败时将 state、reports、docs 备份到 `recoverable-state-<run id>` 工件。请不要删除恢复清单、去重数据库或通知缓存。

## 自动运行和推送

- GitHub Actions 工作流 `journal-alert daily` 每天北京时间 07:30 运行，或在 Actions 页面手动运行。
- Bark **大标题为英文原标题**；正文依次为**中文标题、作者、期刊、中文摘要**，不再显示“忠实翻译”字样。通知末尾保留 DOI 与双语网页链接。
- 如缺英文摘要，会先按 DOI 尝试 OpenAlex、Crossref、Semantic Scholar 与出版社官网公开 Abstract；仍无法取得时，也发送**英文大标题＋中文标题＋作者＋中文“摘要暂缺”说明**，不伪造摘要。若标题翻译服务暂时不可用，该论文留在待推送队列等待翻译成功，避免发送缺少中文标题的通知。
- 如有可信英文摘要，但翻译失败或当天 AI 额度耗尽，先保留待推送队列，下一次再尝试；不会把英文摘要伪装成已完成的中文翻译。
- 通知平台有字节数限制：篇幅太长时会明确显示“正文较长”，附完整页面与原文链接，不能保证一条手机推送容纳完整摘要。
- Bark API 接受请求不等于手机实际接收；程序分别记录每篇论文的服务端确认信息，失败保留重试资格。
- 旧微信/Server酱及 PushPlus 通道和对应发送程序已停止使用并移除。

## RSS 与尽可能补齐论文摘要

目前 52 本期刊**没有配置经验证的 RSS feed**，`config.json` 的 RSS 开关继续保持 `false`。RSS 可能提供更及时的文章，有些源含完整摘要，但也有很多源只有题目或简介，且常缺乏历史记录。因此**RSS 适合逐刊验证后与 OpenAlex/Crossref 并行补充，不适合完全替换它们**。本项目已依 DOI 尝试 OpenAlex → Crossref → Semantic Scholar → 出版社官网公开 Abstract/JSON-LD/正文摘要；无法访问的付费网页或 JavaScript 页面依旧可能缺摘要。

## 关键词依据与学者研究专题

保留原有十组方向，并从韩秀峰、于国强、赵巍胜、万蔡华、何世坤及有论文证据的合作者研究中补充**磁振子量子器件、轨道力矩及晶圆集成、MTJ 概率计算、二维与交替磁隧穿器件**等四组。当前共有 **14 组、183 个短语**。程序支持英文论文标题中不同空格、普通连字符和 Unicode 连字符的匹配，降低 `spin–orbit torque` 等写法差异导致的漏检。

代表文献、合作作者及关键词理由见 [research/keyword_evidence.md](research/keyword_evidence.md)。相关合作者不会仅因名字出现就无条件推送其全部文章，以减少重名误报和跨学科噪声。

**中文标题保障：** 本地翻译缓存可复用；若模型调用额度用尽或翻译不符合基本质量要求，仍保留待发记录，次日继续重试。无法保证 AI 服务故障时当天所有论文都有中文标题。GitHub Actions Variable `JALERT_AI_MAX_CALLS_DAY` 如果以前设置为 `25`，需要按服务额度自行提高；未设置则默认 `200`（调用可能产生费用）。

## 标题翻译失败的诊断与修复

2026-10-21 的 `titles_only` 运行虽显示绿色 Success，但 4 篇带化学式标题被旧数字校验规则误拒绝，导致没有新增中文标题。已修正化学式 Unicode 下标、数学分数检查；并将这 4 篇已对照英文题名的中文翻译保存在 [research/title_corrections.json](research/title_corrections.json)，即使后续重新生成网页也可恢复对应中文标题。原有缓存和卡片文件现已更新为 45/45 篇中文标题。**线上 Pages 需要下次工作流部署后才会刷新。**

## GitHub Actions 操作

在 [Actions](https://github.com/cunlongdong/Journal-alert/actions) 中选择 `journal-alert daily` → `Run workflow` → 分支 `main`。

- 默认模式：抓取并去重、生成报告、补齐翻译、发送 Bark。
- `no_push`：抓取并加入待发队列，本次不推送。
- `flush_only`：只尝试补发待处理的论文（同时恢复缺失英文摘要）；不再次抓取期刊。
- `cards_only`：只重建文献网页；不发 Bark。
- `titles_only`：只为已有卡片中尚无中文标题的论文调用翻译，并更新双语网页；**不会抓取新论文，也不会发送 Bark**。翻译额度不足时继续保留待补标题。
- `abstracts_only`：仅按 `abstract_dois` 指定的 DOI 补齐真实英文摘要、尝试中文翻译并重建网页；**不发送 Bark，不触及去重库**。每篇 DOI 的来源、是否存在完整摘要、翻译状态及失败原因均写入 GitHub Actions Summary，仍未解决时显示 warning 而不会假装已补齐。DOI 以英文逗号分隔（最多 15 个）。例如：
  `10.1016/j.jmmm.2026.174589,10.1103/jlhg-yy14,10.1016/j.jmmm.2026.174575`。
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

## 摘要补全来源与开源借鉴

完整摘要按 DOI 依次参考 OpenAlex、Crossref、Semantic Scholar、**Europe PMC（免费 core 元数据）**和出版社官网公开的 Abstract 区块。此措施增加覆盖范围，但无法保证付费/动态网页的摘要一定可抓取。详细比较 Zotero、Trafilatura、ScholarFetch、paperscraper 与 GROBID，请见 [开源摘要恢复方案](research/abstract_recovery_options.md)。

历史网页仍显示英文标题时，请选 Actions → journal-alert daily → Run workflow → main → **titles_only**，该操作不推送、不清库；随后从 Summary 检查尚未补齐的中文标题。自动每日工作流也会在网页生成前尝试补充遗漏的中文标题。

## 中英文完整性

GitHub Actions 的 **中英文标题与摘要完整性（实际日报）** 区块会列出中文标题、原始英文摘要、中文摘要覆盖数量。英文数据源没有提供某些文章的原始 Abstract 时，不允许通过 AI 捏造原文。程序会使用 DOI 查询 OpenAlex、Crossref、Semantic Scholar 和出版社网页公开元数据。出版社可能要求登录、限制机器人或只通过 JavaScript 展示摘要，所以即使你在浏览器中能看到摘要，也不能保证自动采集一定成功；这种情况下仍发送明确的“摘要暂缺”Bark 通知。

**2026-10-21 已按用户要求清理旧状态与历史日报**，包括 SQLite 去重库、Bark 通知确认、翻译缓存与旧网页。下一次正式运行会重建这些文件，并可能再次向 Bark 推送最近时间窗口内之前已经收过的论文。Git 历史提交仍保留旧版本。

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
