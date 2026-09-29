#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流自检：Action 的引用形态与权限声明（纯标准库，可离线跑）。

    python .github/lint_workflows.py                 # 检查 .github/workflows/ 下全部
    python .github/lint_workflows.py --dir <目录>    # 指定目录

为什么单独一个脚本：「依赖是否已固定到不可变引用」「工作流权限是否已最小化」
这两条一直写在文档里，但**没有任何东西在跑它们** —— 文档里的待办与流水线里的一步，
差别就在这里。规矩只要还停在散文里，就一定会被漏掉。

检查项
------

1. **第三方 Action 必须固定到完整 SHA。** `uses: owner/repo@v1` 里的标签是**可变的** ——
   上游可以把它改指到任何提交，于是「我验过的」与「CI 实际跑的」变成两回事。
   本地路径引用（`./…`）不受此限，但要检查目标存在。
2. **每个工作流都要有顶层 `permissions:`。** 缺这个键时权限取平台默认值，通常比需要的宽。
3. 顶层 `permissions:` 是**空映射**时，**每个 job 都要自己声明权限** ——
   这样将来新增 job 却忘了写权限时，它拿到的是「没有权限」，
   而不是「上一个 job 恰好需要的那些权限」。漏写权限时，默认值应当落在安全的那一侧。

实现说明：工作流是 YAML，但这里**不引第三方 YAML 库**（本仓库是纯标准库的）。
规则只依赖少量结构性信息（顶层键、job 名、`uses:` 行），按缩进读就够；
而且读错的代价是「多报一条」而不是「静默放行」。

退出码：0 = 通过；1 = 有失败项；2 = 用法错误。
"""

import argparse
import os
import re
import sys

SHA_LEN = 40
HEX_SHA_RE = re.compile(r"^[0-9a-f]{%d}$" % SHA_LEN)
# `- uses: owner/repo@<ref>` 后面常跟一个行尾注释（写回它对应的大版本号），
# 所以两处都要容得下：列表符号 `- ` 与 `# …`。
USES_RE = re.compile(r"^\s*(?:-\s+)?uses\s*:\s*(\S+)\s*(?:#.*)?$")
TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
JOB_START_RE = re.compile(r"^  [A-Za-z0-9_-]+\s*:")
JOB_PERMS_RE = re.compile(r"^\s{4}permissions\s*:")
TOP_PERMS_RE = re.compile(r"^permissions\s*:\s*(.*)$")


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def job_blocks(lines, jobs_at):
    """把 `jobs:` 之后的内容切成 [(job 名, 起行, 止行)]（0 基行号）。"""
    blocks = []
    name = start = None
    for i in range(jobs_at + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#"):
            if not line[:1].isspace():
                break                      # 离开 jobs: 段
            if JOB_START_RE.match(line):
                if start is not None:
                    blocks.append((name, start, i - 1))
                name, start = line.strip().split(":", 1)[0], i
    if start is not None:
        blocks.append((name, start, len(lines) - 1))
    return blocks


def lint_file(path, root, problems, passes):
    rel = os.path.basename(path)
    lines = read_text(path).splitlines()

    top_permissions = None                 # None = 没见到；"" = 空映射；其余 = 有内容
    jobs_at = None
    uses_seen = 0
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            m = TOP_PERMS_RE.match(line)
            if m:
                top_permissions = m.group(1).strip()
            m2 = TOP_KEY_RE.match(line)
            if m2 and m2.group(1) == "jobs":
                jobs_at = i
        m3 = USES_RE.match(line)
        if m3:
            uses_seen += 1
            ref = m3.group(1)
            where = "%s:%d" % (rel, i + 1)
            if ref.startswith("./") or ref.startswith(".\\"):
                target = os.path.join(root, ref[2:].split("/")[0])
                if not os.path.exists(target):
                    problems.append("%s 本地 Action 路径不存在：%s" % (where, ref))
            elif "@" not in ref:
                problems.append("%s `uses: %s` 缺少 `@<ref>`" % (where, ref))
            else:
                revision = ref.rpartition("@")[2]
                if not HEX_SHA_RE.match(revision):
                    problems.append(
                        "%s Action 未固定到完整 SHA：`%s`。可变标签可以被上游改指，"
                        "于是「验过的」与「跑着的」就不是同一份代码。"
                        "固定的前提是有人负责升 —— 由依赖更新机器人做。" % (where, ref))

    if uses_seen:
        passes.append("%s：%d 个 `uses:` 已逐个检查引用形态" % (rel, uses_seen))

    if top_permissions is None:
        problems.append("%s 缺少顶层 `permissions:` —— 缺它时权限取平台默认值，"
                        "通常比需要的宽。显式写 `{}` 并在 job 级授予。" % rel)
        return

    passes.append("%s：顶层已声明 `permissions:`" % rel)
    if top_permissions.strip() not in ("{}", "{ }"):
        return

    if jobs_at is None:
        return
    blocks = job_blocks(lines, jobs_at)
    if not blocks:
        problems.append("%s：顶层 `permissions` 为空，但读不到任何 job" % rel)
        return
    missing = [name for name, start, end in blocks
               if not any(JOB_PERMS_RE.match(lines[k]) for k in range(start, end + 1))]
    if missing:
        problems.append("%s：顶层 `permissions` 为空映射，以下 job 未声明权限：%s"
                        % (rel, "、".join(missing)))
    else:
        passes.append("%s：%d 个 job 均已声明权限" % (rel, len(blocks)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lint_workflows.py",
                                 description="工作流自检：Action 引用形态与权限声明")
    ap.add_argument("--dir", default=None, help="工作流目录（默认 <仓库根>/.github/workflows）")
    ap.add_argument("--quiet", action="store_true", help="只输出失败")
    args = ap.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = args.dir or os.path.join(root, ".github", "workflows")
    if not os.path.isdir(target):
        print("错误：%s 不是目录" % target, file=sys.stderr)
        return 2
    # 仓库根按工作流目录的**位置**推断（`<根>/.github/workflows`），
    # 这样传 `--dir` 指向别处时，本地 Action 的路径也按那边解析。
    root = os.path.dirname(os.path.dirname(os.path.abspath(target)))

    files = sorted(f for f in os.listdir(target) if f.endswith((".yml", ".yaml")))
    if not files:
        print("错误：%s 下没有工作流文件" % target, file=sys.stderr)
        return 2

    problems, passes = [], []
    for name in files:
        lint_file(os.path.join(target, name), root, problems, passes)

    if not args.quiet:
        for msg in passes:
            print("  PASS  %s" % msg)
    for msg in problems:
        print("  FAIL  %s" % msg)
    print("\n%d 通过 / %d 失败" % (len(passes), len(problems)))
    if problems:
        print("工作流自检未通过")
        return 1
    print("工作流自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
