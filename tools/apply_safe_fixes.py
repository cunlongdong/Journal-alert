"""Idempotent, non-destructive source fixes for a checked-out Journal-alert tree.

Only edits known source/documentation anchors; never touches state/, reports/,
secrets, the delivery queue, or any SQLite database.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare_changes(root: Path) -> dict[Path, str]:
    root = Path(root)
    result = {}
    cards = root / "jalert" / "cards.py"
    content = cards.read_text(encoding="utf-8")
    before = '''    if not articles:
        if log:
            log.info("cards page skipped: no reports found in %s", reports_dir)
        return None
'''
    after = '''    if not articles:
        if log:
            log.info("cards page rebuilt empty: no reports found in %s", reports_dir)
        # Build an empty HTML dashboard rather than leaving a stale published wall.
'''
    if before in content:
        if content.count(before) != 1:
            raise ValueError("Ambiguous cards.py empty-site branch; refuse to edit")
        result[cards] = content.replace(before, after, 1)
    elif "cards page rebuilt empty: no reports found" not in content and after not in content:
        raise ValueError("cards.py changed unexpectedly; please review the empty-site branch")

    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8")
    old_heading = "# 大气环境文献日报（journal-alert）"
    new_heading = "# 自旋电子学文献日报（Journal-alert）"
    if text.startswith(old_heading):
        text = text.replace(old_heading, new_heading, 1)
        text = text.replace(
            "把「期刊定期推送」变成**按关键词筛选后的微信推送 + 本地 Markdown 每日阅读报**，全自动、每天定时运行。",
            "面向自旋电子学研究的期刊监测系统：自动筛选文献、完整中英文摘要网页，并通过 Server酱和 Bark 续页推送完整中文直译。",
            1,
        )
        add = (
            "\n> **当前云端部署说明（2026-10）**：仓库已配置 52 本期刊及 10 组自旋电子学关键词。\n"
            "> GitHub Actions 使用 `tools/enhanced_run.py` 抓取并以 `--no-push` 禁用旧通道，\n"
            "> 再由 `tools/reliable_push.py` 独立推送 Server酱/Bark。\n"
            "> `config.json` 的早期 `push.channels` 设置仍供原 `run.py` 本地模式使用，\n"
            "> 不代表 GitHub Actions 当前的实际双通道配置；云端配置以 Actions Secrets/Variables 为准。\n"
            "> 原 README 后半部分包含上游项目的旧领域示例，需要逐步对照实际代码使用。\n\n"
        )
        text = text.replace(new_heading + "\n", new_heading + "\n" + add, 1)
        result[readme] = text
    elif text.startswith(new_heading):
        # Accept already updated README.
        pass
    else:
        raise ValueError("README.md header changed; refuse automatic edit")

    flow = root / ".github" / "workflows" / "daily.yml"
    yaml = flow.read_text(encoding="utf-8")
    needle = "      - name: Commit reports and safe resumable state\n"
    if needle in yaml and "      - name: Bilingual completeness audit\n" not in yaml:
        audit = '''      - name: Bilingual completeness audit
        if: ${{ success() && !inputs.delivery_status && !inputs.test_api && !inputs.test_bark }}
        shell: bash
        run: |
          echo '### 网页双语摘要完整性' >> "$GITHUB_STEP_SUMMARY"
          echo '```json' >> "$GITHUB_STEP_SUMMARY"
          python tools/audit_site_fullness.py >> "$GITHUB_STEP_SUMMARY"
          echo '```' >> "$GITHUB_STEP_SUMMARY"
'''
        result[flow] = yaml.replace(needle, audit + needle, 1)
    elif "      - name: Bilingual completeness audit\n" not in yaml:
        raise ValueError("daily.yml commit anchor missing; refuse automatic edit")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply inspected source-only edits")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    updates = prepare_changes(args.root)
    if not updates:
        print("Source fixes already applied; no changes necessary.")
    for path, content in updates.items():
        print(("Applying" if args.apply else "Would update") + ": " + str(path.relative_to(args.root)))
        if args.apply:
            path.write_text(content, encoding="utf-8", newline="\n")
    if not args.apply:
        print("Preview only: nothing has been changed. Add --apply to edit source files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
