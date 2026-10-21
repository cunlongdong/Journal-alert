# 摘要缺失补救：GitHub 开源项目评估（2026-10-21）

## 为什么人工看到 Abstract、自动采集却没有？

Crossref/OpenAlex 记录与出版社网页并不是同一个数据源；出版社页面可能只在 JavaScript 执行后展示摘要，也可能要求机构登录或限制机器人。某些旧日报只有摘要截取片段，不能把片段当完整英文摘要。

## 可参考的 GitHub 项目

| 项目 | 适用方案 | 限制 |
| --- | --- | --- |
| [Zotero translators](https://github.com/zotero/translators) | 出版社专用论文元数据和 Abstract HTML 结构识别（优先参考） | JavaScript 代码不能直接作为 Python 插件，需单独实现并遵守许可 |
| [Trafilatura](https://github.com/adbar/trafilatura) | 从 HTML 提取文章正文/元数据 | 可能把非摘要正文混入结果，必须限定 Abstract 区块 |
| [ScholarFetch](https://github.com/laibniz/scholarfetch) | Elsevier、Springer、Europe PMC、OpenAlex、Crossref 等多 API 汇合 | 部分出版社接口需要 API 密钥/授权 |
| [paperscraper](https://github.com/jannisborn/paperscraper) | arXiv/PubMed/预印本的元数据与合法 OA 文献 | 预印本和正式论文需要严格核对 DOI/版本 |
| [GROBID](https://github.com/grobidOrg/grobid) | 将学术 PDF 解析成包括 Abstract 的结构化文本 | 必须取得合法 PDF、需要更高内存与长期服务 |
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | 多来源查询及 Unpaywall 合法开放 PDF 路径 | Unpaywall 不一定包含摘要，PDF 解析是另一步 |

## 目前已实现（不新增第三方安装依赖）

- 按 DOI 查询 OpenAlex 倒排索引、Crossref、Semantic Scholar。
- 新增 Europe PMC：精确 DOI 查询，并指定 resultType=core 才读取 abstractText。
- DOI 出版社网页：读取 citation_abstract、Dublin Core、JSON-LD 和明确的 HTML Abstract 区块；对返回的 DOI 不匹配内容拒绝。
- 保存来源，只有能证明对应论文的公开摘要才翻译；无摘要时在 Bark 标为暂缺，不能让模型凭空补原文。
- 独立翻译所有旧卡片缺失的中文标题，不要求摘要可用。

## 推荐下一阶段

请先提供 5–10 个“出版社网页能人工看到 Abstract、卡片墙缺完整英文摘要”的 DOI。优先按缺失最多的出版社借鉴 Zotero 的专用适配器；若有 Elsevier/Springer 官方 API 权限，按 ScholarFetch 思路接入。只有获得合法开放 PDF 时再考虑单独部署 GROBID。

无法保证摘要覆盖率 100%；订阅限制、网站反爬、JS 生成和缺失原始元数据都会造成无法自动获取。
