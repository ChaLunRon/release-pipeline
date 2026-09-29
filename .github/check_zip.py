#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前的资产自检：把 zip 与 tag 逐文件对齐。

**为什么比对基准是 Git 对象指纹而不是 zip 的哈希**：zip 的条目时间戳取构建机器
所在时区的时间，同一个 tag 在 UTC 与 GMT+8 打出的容器字节不同（差异数 = 条目数 × 2，
local file header 与 central directory 各一处）。把断言打在容器哈希上，
会得到一个**随机变红**的流水线。指纹比对与平台、行尾、时区全部无关。

这一条同时能挡住行尾漂移：曾经有文件只被 `* text=auto` 覆盖、没有显式 `eol`，
`git archive` 在不同平台上产出 CRLF / LF —— 同一个 tag 会打出两个内容不同的包。

用法：python .github/check_zip.py <zip路径> <顶层目录名> <tag>
退出码：0 = 一致；1 = 不一致。
"""

import hashlib
import re
import subprocess
import sys
import zipfile

# 与远端 trees API 的 blob sha 同口径。注意对象头里的分隔符是**空字节**。
BLOB_HEADER = b"blob %d\0"

NAME_CHARSET = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def blob_sha(data):
    return hashlib.sha1(BLOB_HEADER % len(data) + data).hexdigest()


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    zip_path, name, tag = argv[1], argv[2], argv[3]

    if not NAME_CHARSET.match(name):
        print("!! 顶层目录名不符合技能 name 的字符集要求：%r" % name)
        return 1

    with zipfile.ZipFile(zip_path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}

    tops = {n.split("/")[0] for n in entries}
    if tops != {name}:
        print("!! zip 顶层目录不唯一或不等于 name：%s" % sorted(tops))
        return 1
    if name + "/SKILL.md" not in entries:
        print("!! zip 里找不到 %s/SKILL.md" % name)
        return 1
    if any(".git/" in n for n in entries):
        print("!! zip 里混进了 .git")
        return 1
    if any("__pycache__" in n or n.endswith(".pyc") for n in entries):
        print("!! zip 里混进了 Python 缓存")
        return 1

    tree = subprocess.run(["git", "ls-tree", "-r", "--full-tree", tag],
                          capture_output=True, text=True, check=False)
    if tree.returncode != 0:
        print("!! 读不到 tag %s 的树 —— 请在**仓库根目录**下运行，并确认该 tag 存在：\n%s"
              % (tag, (tree.stderr or "").strip()))
        return 1
    expected = {}
    for line in tree.stdout.splitlines():
        meta, path = line.split("\t", 1)
        expected[path] = meta.split()[2]

    got = {n.split("/", 1)[1]: blob_sha(d) for n, d in entries.items()}

    only_zip = sorted(set(got) - set(expected))
    only_tag = sorted(set(expected) - set(got))
    differ = sorted(p for p in set(got) & set(expected) if got[p] != expected[p])

    # 永远分三栏报告：独有路径从前两栏看，内容差异从第三栏看。
    # 只报「不一致」而不分栏，会把「路径前缀写错」误报成「内容全错」。
    print("zip 条目 %d / tag 文件 %d" % (len(got), len(expected)))
    print("zip 独有 %d / tag 独有 %d / 内容不同 %d" % (len(only_zip), len(only_tag), len(differ)))
    if only_zip or only_tag or differ:
        print("!! 不一致：%s" % (only_zip + only_tag + differ)[:12])
        return 1

    print("通过：顶层 %s，与 tag %s 逐文件内容一致" % (name, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
