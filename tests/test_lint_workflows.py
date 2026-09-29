#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`.github/lint_workflows.py` 的离线单元测试。

不联网、不读真实工作流：每个用例都在临时目录里现造一份最小工作流。
用例里的 SHA 是形态正确的**假值**（只用来验证「形态判断」这一件事），
不是任何真实提交 —— 别把它当成可以照抄的固定值。
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".github"))

import lint_workflows as lw  # noqa: E402

FAKE_SHA = "0123456789abcdef" * 2 + "01234567"      # 40 位十六进制，非真实提交

FULLY_PINNED = """\
name: demo
on: [push]
permissions: {}

jobs:
  build:
    runs-on: ubuntu-22.04
    permissions:
      contents: read
    steps:
      - uses: owner/action@%s  # v1
      - run: echo hi
""" % FAKE_SHA


def write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lintwf-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_lint(self, text, name="demo.yml"):
        write(os.path.join(self.tmp, ".github", "workflows", name), text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = lw.main(["--dir", os.path.join(self.tmp, ".github", "workflows")])
        return code, buf.getvalue()


class TestUsesPinning(Base):
    def test_fully_pinned_passes(self):
        code, out = self.run_lint(FULLY_PINNED)
        self.assertEqual(code, 0, out)
        self.assertIn("工作流自检通过", out)

    def test_moving_tag_is_a_failure(self):
        """可变标签可被上游改指 —— 「我验过的」与「CI 跑的」会变成两份代码。"""
        code, out = self.run_lint(FULLY_PINNED.replace("@" + FAKE_SHA, "@v1"))
        self.assertEqual(code, 1)
        self.assertIn("未固定到完整 SHA", out)

    def test_branch_ref_is_a_failure(self):
        code, out = self.run_lint(FULLY_PINNED.replace("@" + FAKE_SHA, "@main"))
        self.assertEqual(code, 1)
        self.assertIn("未固定到完整 SHA", out)

    def test_short_sha_is_a_failure(self):
        code, out = self.run_lint(FULLY_PINNED.replace("@" + FAKE_SHA, "@" + FAKE_SHA[:7]))
        self.assertEqual(code, 1)
        self.assertIn("未固定到完整 SHA", out)

    def test_missing_ref_is_a_failure(self):
        code, out = self.run_lint(FULLY_PINNED.replace("@" + FAKE_SHA, ""))
        self.assertEqual(code, 1)
        self.assertIn("缺少 `@<ref>`", out)

    def test_local_action_needs_existing_path(self):
        text = FULLY_PINNED.replace("owner/action@" + FAKE_SHA, "./missing-action")
        code, out = self.run_lint(text)
        self.assertEqual(code, 1)
        self.assertIn("本地 Action 路径不存在", out)

    def test_local_action_with_existing_path_passes(self):
        os.makedirs(os.path.join(self.tmp, "local-action"))
        text = FULLY_PINNED.replace("owner/action@" + FAKE_SHA, "./local-action")
        code, out = self.run_lint(text)
        self.assertEqual(code, 0, out)

    def test_line_comment_after_ref_is_tolerated(self):
        """SHA 后面常跟一个写回大版本号的行尾注释 —— 不能被当成 ref 的一部分。"""
        code, out = self.run_lint(FULLY_PINNED.replace("# v1", "# v1 由机器人升"))
        self.assertEqual(code, 0, out)


class TestPermissions(Base):
    def test_missing_top_level_permissions_fails(self):
        code, out = self.run_lint(FULLY_PINNED.replace("permissions: {}\n", ""))
        self.assertEqual(code, 1)
        self.assertIn("缺少顶层 `permissions:`", out)

    def test_job_without_permissions_fails_when_top_is_empty(self):
        """顶层为空映射时，每个 job 都要自己声明 —— 漏写的默认值必须落在安全那一侧。"""
        text = FULLY_PINNED.replace("    permissions:\n      contents: read\n", "")
        code, out = self.run_lint(text)
        self.assertEqual(code, 1)
        self.assertIn("以下 job 未声明权限：build", out)

    def test_non_empty_top_level_permissions_is_enough(self):
        """顶层直接给了具体权限时，job 不必再声明。"""
        text = FULLY_PINNED.replace("permissions: {}", "permissions:\n  contents: read")
        text = text.replace("    permissions:\n      contents: read\n", "")
        code, out = self.run_lint(text)
        self.assertEqual(code, 0, out)

    def test_multiple_jobs_are_checked_individually(self):
        text = FULLY_PINNED + """
  second:
    runs-on: ubuntu-22.04
    steps:
      - run: echo hi
"""
        code, out = self.run_lint(text)
        self.assertEqual(code, 1)
        self.assertIn("second", out)
        self.assertNotIn("未声明权限：build", out)


class TestCliContract(Base):
    def test_missing_directory_returns_code_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = lw.main(["--dir", os.path.join(self.tmp, "nope")])
        self.assertEqual(code, 2)

    def test_empty_directory_returns_code_2(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = lw.main(["--dir", empty])
        self.assertEqual(code, 2)

    def test_quiet_suppresses_passes(self):
        write(os.path.join(self.tmp, ".github", "workflows", "demo.yml"), FULLY_PINNED)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = lw.main(["--dir", os.path.join(self.tmp, ".github", "workflows"),
                            "--quiet"])
        self.assertEqual(code, 0)
        self.assertNotIn("PASS", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
