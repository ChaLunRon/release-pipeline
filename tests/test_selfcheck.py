#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck.py 的离线单元测试。

**夹具里的违规样例一律用字符串拼接构造**（如盘符、分隔符、地址分段拼起来），
理由有二：

1. 这些样例如果写成完整字面量，本仓库自己的自检就会把它们判成违规 —— 自相矛盾；
2. 未来的全局替换（改名、换署名）会把完整字面量的常量一起改坏，而检查器会**静默失效**。

测试不联网。
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
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import selfcheck  # noqa: E402

SEP = chr(92)                                   # 反斜杠，拼接构造
DRIVE = "C" + ":" + SEP + "Users" + SEP + "someone" + SEP + "file.txt"
UNIX_HOME = "/" + "Users" + "/" + "someone" + "/" + "file.txt"
PLAIN_IP = ".".join(["8", "8", "8", "8"])
LOOPBACK = ".".join(["127", "0", "0", "1"])
DOC_RANGE_IP = ".".join(["192", "0", "2", "9"])
ENV_WORD = "本" + "机"


def write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def make_package(tmp, name="demo-skill", version="1.0", body="# 演示\n\n正文。\n",
                 changelog_version=None, readme_version=None, extras=None):
    root = os.path.join(tmp, name)
    os.makedirs(root)
    write(os.path.join(root, "SKILL.md"),
          "---\nname: %s\ndescription: 一个用于测试的技能。\nlicense: MIT\n"
          "metadata:\n  version: \"%s\"\nagent_created: true\n---\n\n%s" % (name, version, body))
    write(os.path.join(root, "README.md"),
          "![v](https://img.shields.io/badge/version-%s-blue)\n\n## 用法\n\n见 [用法](#用法)。\n"
          % (readme_version or version))
    write(os.path.join(root, "CHANGELOG.md"), "## %s\n\n初始版本。\n"
          % (changelog_version or version))
    if extras:
        for rel, text in extras.items():
            write(os.path.join(root, rel), text)
    return root


def run(root, quiet=False):
    buf = io.StringIO()
    argv = [root] + (["--quiet"] if quiet else [])
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = selfcheck.main(argv)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfcheck-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestStructure(Base):
    def test_valid_package_passes(self):
        root = make_package(self.tmp)
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("自检通过", out)

    def test_name_must_match_directory(self):
        root = make_package(self.tmp, name="demo-skill")
        os.rename(root, os.path.join(self.tmp, "other-name"))
        code, out = run(os.path.join(self.tmp, "other-name"))
        self.assertEqual(code, 1)
        self.assertIn("必须与所在目录名", out)

    def test_name_rejects_dots(self):
        root = make_package(self.tmp, name="demo-skill")
        write(os.path.join(root, "SKILL.md"),
              "---\nname: demo-skill-1.0\ndescription: 测试。\n---\n\n正文\n")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("不得有点号", out)

    def test_reserved_word_in_name(self):
        root = make_package(self.tmp, name="claude-thing")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("保留字", out)

    def test_missing_description(self):
        root = make_package(self.tmp)
        write(os.path.join(root, "SKILL.md"), "---\nname: demo-skill\n---\n\n正文\n")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("description", out)

    def test_missing_skill_md_returns_code_1(self):
        root = os.path.join(self.tmp, "empty-skill")
        os.makedirs(root)
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("找不到 SKILL.md", out)

    def test_not_a_directory_returns_code_2(self):
        code, out = run(os.path.join(self.tmp, "does-not-exist"))
        self.assertEqual(code, 2)


class TestVersionConsistency(Base):
    def test_changelog_version_mismatch(self):
        root = make_package(self.tmp, changelog_version="1.1")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("CHANGELOG.md 最新小节", out)

    def test_readme_badge_version_mismatch(self):
        root = make_package(self.tmp, readme_version="1.2")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("徽章版本", out)

    def test_matching_versions_pass(self):
        root = make_package(self.tmp)
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("版本一致", out)


class TestVersionChannel(Base):
    """版本号形态**由分发渠道决定**。（见 references/channel-boundaries.md）

    两个渠道各有自己的规矩，不是「同一个问题的两种解法」：
    本地 / GitHub 通道用两段式 `主.次`；平台上传通道用三段式 SemVer。
    拿一个渠道的规则去判另一个渠道的包，会得到一个**假 FAIL**。
    """

    @staticmethod
    def make_platform_package(tmp, version="1.0.0", name="demo-skill"):
        root = os.path.join(tmp, name)
        os.makedirs(root)
        write(os.path.join(root, "SKILL.md"),
              "---\nname: %s\nversion: \"%s\"\ndisplay_name: 演示\ndisplay_name_en: Demo\n"
              "description_zh: 中文描述。\ndescription_en: |\n  English description.\n"
              "description: 演示技能。\n---\n\n正文。\n" % (name, version))
        write(os.path.join(root, "README.md"),
              "![v](https://img.shields.io/badge/version-%s-blue)\n" % version)
        write(os.path.join(root, "CHANGELOG.md"), "## %s\n\n初始版本。\n" % version)
        return root

    def test_platform_package_with_semver_passes(self):
        root = self.make_platform_package(self.tmp)
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("渠道 platform", out)

    def test_semver_platform_package_is_not_rejected(self):
        """回归：原先只认 `主.次`，会把平台形态的 `1.0.0` 判成 FAIL —— 假 FAIL。"""
        root = self.make_platform_package(self.tmp)
        _code, out = run(root)
        self.assertNotIn("应为", out)

    def test_local_package_with_semver_fails(self):
        root = make_package(self.tmp, version="1.0.0")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("主.次（本地 / GitHub 通道）", out)

    def test_channel_can_be_forced(self):
        root = self.make_platform_package(self.tmp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = selfcheck.main([root, "--channel", "local"])
        self.assertEqual(code, 1)

    def test_block_scalar_description_is_measured(self):
        """描述写成块标量（`description: |`）时也要量出真实长度 ——
        否则一段一千多字的描述会被读成两个字符，「超限」被静默放过。"""
        root = os.path.join(self.tmp, "demo-skill")
        os.makedirs(root)
        long_desc = "触发词 " * 400
        write(os.path.join(root, "SKILL.md"),
              "---\nname: demo-skill\ndescription: |\n  %s\n---\n\n正文。\n" % long_desc)
        expected = len(" ".join(long_desc.split()))
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("长度 %d > 1000" % expected, out)

    def test_description_over_limit_is_a_failure(self):
        """上限是 1000，不是 1024 —— 依据是平台自己的报错原文。"""
        root = os.path.join(self.tmp, "demo-skill")
        os.makedirs(root)
        write(os.path.join(root, "SKILL.md"),
              "---\nname: demo-skill\ndescription: %s\n---\n\n正文。\n" % ("字" * 1010))
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("长度 1010 > 1000", out)


class TestLinksAndReferences(Base):
    def test_broken_relative_link(self):
        root = make_package(self.tmp, extras={
            "docs/a.md": "见 [不存在](missing.md)。\n"})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("链接目标不存在", out)

    def test_bad_anchor(self):
        root = make_package(self.tmp, extras={
            "docs/a.md": "见 [锚点](./b.md#没有这个标题)。\n",
            "docs/b.md": "# 别的标题\n"})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("锚点", out)

    def test_good_anchor_passes(self):
        root = make_package(self.tmp, extras={
            "docs/a.md": "见 [锚点](./b.md#目标标题)。\n",
            "docs/b.md": "# 目标标题\n"})
        code, out = run(root)
        self.assertEqual(code, 0, out)

    def test_nested_reference_detected(self):
        root = make_package(self.tmp, extras={
            "references/one.md": "详见 two.md 的说明。\n",
            "references/two.md": "内容。\n"})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("引用不得嵌套", out)

    def test_long_reference_requires_toc(self):
        root = make_package(self.tmp, extras={
            "references/long.md": "".join("第 %d 行\n" % i for i in range(140))})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("目录", out)

    def test_long_reference_with_toc_passes(self):
        root = make_package(self.tmp, extras={
            "references/long.md": "## 目录\n\n" + "".join("第 %d 行\n" % i for i in range(140))})
        code, out = run(root)
        self.assertEqual(code, 0, out)


class TestHygiene(Base):
    def test_crlf_detected(self):
        root = make_package(self.tmp)
        with open(os.path.join(root, "docs-crlf.md"), "wb") as fh:
            fh.write("# 标题\r\n\r\n正文\r\n".encode("utf-8"))
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("CRLF", out)

    def test_pycache_detected(self):
        root = make_package(self.tmp)
        write(os.path.join(root, "scripts", "__pycache__", "x.pyc"), "x")
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("缓存残留", out)

    def test_broken_script_detected(self):
        root = make_package(self.tmp, extras={"scripts/bad.py": "def f(:\n"})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("语法错误", out)

    def test_placeholder_counted_as_warning_only(self):
        root = make_package(self.tmp, extras={
            "docs/t.md": "把 " + "<" + "your-name" + ">" + " 换成真名。\n"})
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("占位符", out)


class TestPortabilityRules(Base):
    """三条可移植性规则 —— 本工具的核心增量。"""

    def test_drive_path_is_failure(self):
        root = make_package(self.tmp, extras={"docs/env.md": "安装到 %s 下。\n" % DRIVE})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("硬编码的绝对路径", out)

    def test_unix_home_path_is_failure(self):
        root = make_package(self.tmp, extras={"docs/env.md": "配置在 %s。\n" % UNIX_HOME})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("硬编码的绝对路径", out)

    def test_placeholder_path_is_allowed(self):
        root = make_package(self.tmp, extras={
            "docs/env.md": "配置在 $HOME/.config 或 %USERPROFILE% 下。\n"})
        code, out = run(root)
        self.assertEqual(code, 0, out)

    def test_regex_literal_is_not_a_drive_path(self):
        """假阳性回归：`sed 's/\\(https:\\/\\/…\\)/…/'` 里的 `s:\\` 曾被当成盘符。

        这条是本仓库 dogfood 时抓到的（拿新写的检查器去扫另一个技能，
        结果在一处正则字面量上报了假阳性）。规则太爱误报就会被关掉，
        所以收紧它并把这条写成回归用例。
        """
        snippet = "`git push … | sed 's/\\(https:" + SEP + SEP + "…\\)/…/'`\n"
        root = make_package(self.tmp, extras={"docs/pipe.md": snippet})
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertNotIn("硬编码的绝对路径", out)

    def test_plain_ip_is_failure(self):
        root = make_package(self.tmp, extras={"docs/net.md": "解析到 %s。\n" % PLAIN_IP})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("IP 字面量", out)

    def test_loopback_is_allowed(self):
        root = make_package(self.tmp, extras={"docs/net.md": "本地中间人监听 %s。\n" % LOOPBACK})
        code, out = run(root)
        self.assertEqual(code, 0, out)

    def test_documentation_range_is_allowed(self):
        root = make_package(self.tmp, extras={"docs/net.md": "示例地址 %s 仅用于文档。\n" % DOC_RANGE_IP})
        code, out = run(root)
        self.assertEqual(code, 0, out)

    def test_env_reference_is_warning_not_failure(self):
        root = make_package(self.tmp, extras={"docs/env.md": "%s 上读不到。\n" % ENV_WORD})
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("第一人称环境指代", out)

    def test_env_reference_can_be_exempted(self):
        root = make_package(self.tmp, extras={
            "docs/env.md": "「%s」这类词不能用。 <!-- %s: 举例 -->\n"
                           % (ENV_WORD, selfcheck.EXEMPT_MARKER)})
        code, out = run(root)
        self.assertEqual(code, 0, out)
        self.assertIn("豁免标记 1 处", out)
        self.assertNotIn("第一人称环境指代（请逐条判断", out)

    def test_ip_rule_scans_scripts_too(self):
        root = make_package(self.tmp, extras={
            "scripts/x.py": "HOST = \"%s\"\n" % PLAIN_IP})
        code, out = run(root)
        self.assertEqual(code, 1)
        self.assertIn("IP 字面量", out)


class TestCheckerSelfConsistency(unittest.TestCase):
    """检查器自身不得含有被检查字面量的完整形态 —— 否则全局替换会让它静默失效。"""

    def setUp(self):
        with open(os.path.join(ROOT, "scripts", "selfcheck.py"), "r", encoding="utf-8") as fh:
            self.src = fh.read()

    def test_source_has_no_env_word(self):
        for word in selfcheck.ENV_REFERENCE_WORDS:
            self.assertNotIn(word, self.src, "自检器源码里出现了被禁词表字面量：%r" % word)

    def test_source_does_not_match_its_own_path_rule(self):
        self.assertIsNone(selfcheck.MACHINE_PATH_RE.search(self.src),
                          "自检器源码被自己的「本机绝对路径」规则命中")

    def test_source_does_not_match_its_own_ip_rule(self):
        for m in selfcheck.IPV4_RE.finditer(self.src):
            ip = m.group(0)
            self.assertTrue(ip.startswith(selfcheck.IPV4_ALLOWED_PREFIXES)
                            or ip.startswith(selfcheck.IPV4_ALLOWED_NETWORKS),
                            "自检器源码里出现了白名单外的地址字面量：%s" % ip)

    def test_source_has_no_placeholder_literal(self):
        for token in selfcheck.PLACEHOLDER_TOKENS:
            self.assertNotIn(token, self.src)

    def test_rules_use_concatenation(self):
        """规则常量必须是拼接形态 —— 直接断言拼接片段存在，防止有人「顺手简化」回去。"""
        self.assertIn('"Users" + "|" + "home"', self.src)
        self.assertIn('"本" + "机"', self.src)

    def test_shipped_repo_exemption_count_is_minimal(self):
        """豁免标记是「有意保留的举例行」。数量上涨 = 有人在拿它绕过可移植性检查。"""
        count = 0
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                with open(os.path.join(dirpath, name), "r", encoding="utf-8") as fh:
                    for line in fh:
                        if selfcheck.EXEMPT_MARKER in line:
                            count += 1
        self.assertLessEqual(
            count, 2, "豁免标记涨到 %d 处 —— 先确认是「必须举例说明被禁写法」，"
                      "而不是拿它绕过检查" % count)


if __name__ == "__main__":
    unittest.main()
