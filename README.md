# 自旋电子学文献日报 · Journal-alert

Journal-alert 检索 52 本已启用期刊，通过 OpenAlex 和 Crossref 采集文献，按自旋电子学关键词筛选并去重，生成 Markdown 日报与 GitHub Pages 中英文卡片。**唯一通知渠道为 Bark**。

## 历史与持久化

本仓库是 `pkui1mpression-design/Journal-alert` 的 fork，保留原项目 RSS / OpenAlex / Crossref 多源检索、关键词评分、SQLite DOI 去重、按日 Markdown 报告和状态入库，并在此基础上实现 Bark 逐篇中英双语推送、DOI 原始摘要保存、中文翻译和 GitHub Pages 卡片墙。

**不要为了排查无推送而反复删除历史库。** `state/seen.sqlite` 是跨天去重依据，`state/enhanced_wechat_outbox.json` 记录每篇论文的 Bark 确认状态；删掉任何一个都可能重发旧论文。`state/article_metadata.json` 与 `reports/YYYY-MM-DD.json` 持久化保留全文元数据，`state/enhanced_zh_cache.json` 保存中文译文；摘要质量待补齐时会继续重试。

历史库在此前按要求清空，当前代码没有再执行清库。只有确实需重新建库时，才使用显式的验收保护 `state/rebuild_review_hold.json`，在人工确认之前阻止 Bark 重发；`approve_rebuild` 模式只解锁、不推送。工作流正常运行时没有该保护文件。

## 每天 09:00 自动运行：GitHub fork 的必要设置

**原作者的实现**：`.github/workflows/daily.yml` 使用 `on.schedule`，提交日报及去重库；上游 README 还记录过 GitHub 定时任务严重延迟（曾直到 11:16 才启动），因此原作者另外使用 Windows 计划任务进行多时段补跑。fork 继续保留 GitHub 云端运行，避免依赖电脑开机。

**本 fork 的关键问题**：仓库确实是公开原仓库的 fork，GitHub 对公开 fork 的 scheduled workflows 默认禁用。此前有多次手动 `workflow_dispatch` 成功，但没有任何 `schedule` 事件；证明的只是代码和人工运行可用，**不能证明每天自动执行已经启用**。

**请务必人工执行一次**：在 [Actions → journal-alert daily](https://github.com/cunlongdong/Journal-alert/actions/workflows/daily.yml) 查看是否出现 **Enable workflow**；若有则点击。并在 [Settings → Actions → General](https://github.com/cunlongdong/Journal-alert/settings/actions) 确认 GitHub Actions 允许运行。无权查看/修改 fork 的启用状态时，改 cron、提交代码以及单元测试均无法代替这一 GitHub 平台操作。

现在使用三个不在 UTC 整点重合的实际定时机会：
- **主运行：北京时间每天 09:00**（UTC 01:00）。
- **第一次补跑：09:23**（UTC 01:23）。
- **第二次补跑：11:37**（UTC 03:37）。

同一文献按 DOI 持久化去重，已被 Bark 接受的通知也保留逐篇确认；补跑可以补缺漏，但不会向同一个 DOI 重复推送。补跑会增加 API 查询量，AI 调用仍有每日上限。GitHub 自身不能保证精确启动时间：如果主任务或补跑都未触发，还是要确认 workflow 在 fork 上处于启用状态。

每次真实启动时，GitHub Actions Summary 的**自动任务实际触发情况**都会记录 `schedule` 或 `workflow_dispatch`、实际起始时间及分支。没有新的运行记录说明问题在触发层，不能从 Bark 日志排查。

## 自动运行和推送

**定时运行排障（重要）**：代码里的 `on.schedule` 只是在 GitHub 注册触发时间，不能证明 GitHub 真正启动了任务。到 Actions → `journal-alert daily` 查看运行的 `event` 是否为 `schedule`；如果始终只有 `workflow_dispatch` 和 `push`，需要在 GitHub Actions 页面检查该工作流是否被禁用、是否出现 **Enable workflow**，以及仓库的 Actions 权限。重新启用后建议手动运行一次验证 API、Bark 与数据库写回。纯改 cron 不能强迫 GitHub 调度，因此不要把“离线测试成功”当成“每日通知成功”。

**对照原项目**：本仓库 fork 自 `pkui1mpression-design/Journal-alert`，保留了 RSS/OpenAlex/Crossref 多源采集、按 DOI 去重、关键词分级和本地报告的核心设计。由于当前筛选 52 本期刊，OpenAlex 与 Crossref 对同一期刊同时抓取（每刊最多两个并发源），但保留了分页、独立错误记录及原顺序合并；原始 API 摘要与专业领域的中文逐篇 Bark、双语卡片、失败回补仍由 fork 提供。


- GitHub Actions 的 **09:00 主任务 + 09:23 / 11:37 补跑** 均使用同一个已确认 Bark 的去重队列；首次必须在 fork 的 Actions 页面启用定时工作流。
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
