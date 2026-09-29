#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包自检工具（纯标准库，无第三方依赖）。

用途：在一个技能仓库里跑一遍结构与规范检查，任何一条不通过都以非零退出码结束，
可直接作为 CI 的校验步骤。

    python scripts/selfcheck.py .            # 校验当前目录
    python scripts/selfcheck.py . --quiet    # 只输出问题

检查项
------

结构规范（失败即不合格）：

  1. SKILL.md 存在且含合法的 YAML frontmatter
  2. `name`：仅小写字母/数字/连字符、不以连字符开头结尾、无连续连字符、
     <=64 字符、不含保留字、**与所在目录名一致**
  3. `description`：存在、<=1024 字符、不含 XML 标签、不用第一人称开头
  4. SKILL.md 正文（不含 frontmatter）行数 < 500
  5. references/ 下超过 100 行的文件必须带 `## 目录`
  6. references/ 之间**不得互相引用**（引用只能有一层深度）
  7. 全部相对 Markdown 链接可达；行内锚点按 GitHub 算法可解析
  8. 所有文本文件行尾为 LF（无 CRLF）
  9. scripts/ 下每个 .py 都能编译
 10. 版本一致性：`metadata.version` == CHANGELOG 最新小节号 == README 徽章版本串
 11. 工作区没有 `__pycache__` / `*.pyc` 残留（打包前必须清干净）

可移植性（本工具的**核心增量**，对应「换一台机器使用」这个真实需求）：

 12. **执行环境专属的绝对路径**（FAIL）：文档与配置里出现「盘符 + 反斜杠」路径，
     或 `/Users/<用户名>`、`/home/<用户名>` 形态的家目录绝对路径。
 13. **地址字面量**（FAIL）：任何文件里出现 IPv4 字面量，且不在白名单内
     （白名单 = 回环地址 + 三组文档保留段）。把某台机器/某个网络的实测地址
     写进包里，换台机器就是错的信息。
 14. **第一人称环境指代**（WARN）：文档里出现被禁的词表。
     没有先行词的「第一人称环境指代」会把作者的观测误当成读者自己的处境；
     确实需要举例说明被禁写法的行，可加行内豁免标记跳过（标记数量会被打印）。


两条设计纪律（改这个文件前先读）
--------------------------------

**一、检查字面量的代码，自身不得含有该字面量的完整形态。**
本文件里凡是「要找的东西」，一律用**字符串拼接**构造（`"本" + "机"`、
`"Users" + "|" + "home"` 之类）。理由：将来做一次全局替换（改名、换署名、换域名）
会把完整形态的常量一起替换掉，检查器随即**静默失效** —— 它照样打印「通过」，
只是再也查不出任何东西。

**二、豁免必须是显式的、可数的。**
`EXEMPT_MARKER` 允许极少数「必须举例说明被禁写法」的行跳过规则 14，
但自检器会把标记数量打印出来。数量上涨 = 有人在拿它绕过检查。

退出码：0 = 通过；1 = 有失败项；2 = 用法错误。
"""

import argparse
import os
import re
import sys

# 官方规范里点名保留的标识（`skill` 不在其中 —— 以 skill- 开头的技能名是合法的）。
RESERVED_WORDS = ("claude", "anthropic", "agent-skills")
MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024
MAX_SKILL_BODY_LINES = 500
TOC_MIN_LINES = 100

# ---------------------------------------------------------------- 规则 12/13/14

# 反斜杠、家目录名、被禁词表 —— 全部拼接构造，避免全局替换污染本文件。
BACKSLASH = chr(92)
HOME_DIR_NAMES = "Users" + "|" + "home"
# 盘符路径的两个约束都是**防假阳性**踩出来的：
#   ① 盘符前不能是字母/数字 —— 否则 `sed 's/\(https:\/\/…\)/…/'` 里的 `s:\` 会被当成盘符；
#   ② 反斜杠后必须紧跟路径字符 —— 否则 `s:\/` 这种转义写法的反斜杠后会跟到 `/`。
DRIVE_PATH = r"(?<![A-Za-z0-9])[A-Za-z]:" + re.escape(BACKSLASH) + r"[A-Za-z0-9_.]"
MACHINE_PATH_RE = re.compile(DRIVE_PATH + r"|/(?:" + HOME_DIR_NAMES + r")/[A-Za-z0-9._-]+")

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# 白名单：回环地址 + RFC 5737 的三组文档保留段（它们永远不会是真实主机的地址）。
IPV4_ALLOWED_PREFIXES = ("127.", "0.0.0.0", "255.255.255.255")
IPV4_ALLOWED_NETWORKS = ("192.0.2.", "198.51.100.", "203.0.113.")

ENV_REFERENCE_WORDS = ("本" + "机", "我这台" + "机器", "我" + "的电脑", "实测" + "环境")
EXEMPT_MARKER = "portability-exempt"

# 规则作用范围：文档与配置按文本规则扫；源码只在结构规则里扫。
PORTABILITY_SUFFIXES = (".md", ".txt", ".yml", ".yaml", ".cff", ".json", ".toml")
DOC_SUFFIXES = (".md",)

PLACEHOLDER_DOMAIN_RE = re.compile(r"example\.(?:com|org|net)")
PLACEHOLDER_TOKENS = (
    "<" + "your-name" + ">",
    "<" + "YOUR_NAME" + ">",
    "<" + "你的用户名" + ">",
)
PLACEHOLDER_SKIP_FILES = {"CHANGELOG.md"}

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
CACHE_SUFFIXES = (".pyc", ".pyo")


class Report:
    def __init__(self):
        self.passes = []
        self.warns = []
        self.fails = []

    def ok(self, msg):
        self.passes.append(msg)

    def warn(self, msg):
        self.warns.append(msg)

    def fail(self, msg):
        self.fails.append(msg)


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def iter_files(root, suffixes=None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if suffixes and not name.endswith(suffixes):
                continue
            yield os.path.join(dirpath, name)


def rel(root, path):
    return os.path.relpath(path, root).replace(BACKSLASH, "/")


def parse_frontmatter(text):
    """极简 frontmatter 解析：顶层键 + 一层缩进子键。"""
    if not text.startswith("---"):
        return None, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return None, text
    raw = parts[0][3:]
    body = parts[1].lstrip("\n")
    data = {}
    current = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] not in (" ", "\t"):
            m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                if val == "":
                    data[key] = {}
                    current = key
                else:
                    data[key] = val.strip("'\"")
                    current = None
        elif current is not None:
            m = re.match(r"^\s+([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
            if m and isinstance(data.get(current), dict):
                data[current][m.group(1)] = m.group(2).strip().strip("'\"")
    return data, body


def github_anchor(heading):
    """GitHub 标题锚点算法：小写 -> 去掉非 [\\w\\s-] 字符 -> 空格转连字符。"""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def collect_anchors(text):
    seen = {}
    anchors = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if not m:
            continue
        base = github_anchor(m.group(2))
        n = seen.get(base, 0)
        anchors.add(base if n == 0 else "%s-%d" % (base, n))
        seen[base] = n + 1
    return anchors


# ------------------------------------------------------------------- 结构规则


def check_frontmatter(rep, root):
    path = os.path.join(root, "SKILL.md")
    if not os.path.isfile(path):
        rep.fail("根目录下找不到 SKILL.md")
        return None, None, None
    text = read_text(path)
    fm, body = parse_frontmatter(text)
    if fm is None:
        rep.fail("SKILL.md 缺少合法 frontmatter（应以 `---` 开头并以 `---` 结束）")
        return {}, body, None
    rep.ok("frontmatter 解析成功")
    return fm, body, text


def check_name(rep, fm, root):
    name = fm.get("name")
    if not name:
        rep.fail("frontmatter 缺少 `name` 字段")
        return None
    if not isinstance(name, str):
        rep.fail("`name` 必须是字符串")
        return None
    if len(name) > MAX_NAME_LEN:
        rep.fail("`name` 长度 %d > %d" % (len(name), MAX_NAME_LEN))
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        rep.fail("`name` 必须是小写字母/数字/连字符（不得有点号、不得首尾连字符、"
                 "不得连续连字符）：%r" % name)
    else:
        rep.ok("`name` 字符集合法：%s" % name)
    actual = os.path.basename(os.path.abspath(root))
    if name != actual:
        rep.fail("`name`（%s）必须与所在目录名（%s）一致。\n"
                 "        常见成因：从平台克隆/检出时目录用的是**仓库名**（通常不带版本后缀）。\n"
                 "        用例：`git clone <url> %s`，或在检出后把目录改名为 %s（CI 的做法见"
                 " .github/workflows/validate.yml）。" % (name, actual, name, name))
    else:
        rep.ok("`name` 与所在目录名一致：%s" % name)
    hit = [w for w in RESERVED_WORDS if w in name.lower()]
    if hit:
        rep.fail("`name` 含保留字：%s" % hit)
    return name


def check_description(rep, fm):
    desc = fm.get("description")
    if not desc:
        rep.fail("frontmatter 缺少 `description` 字段")
        return
    if len(desc) > MAX_DESC_LEN:
        rep.fail("`description` 长度 %d > %d" % (len(desc), MAX_DESC_LEN))
    else:
        rep.ok("`description` 长度 %d / %d" % (len(desc), MAX_DESC_LEN))
    if re.search(r"<[A-Za-z/]", desc):
        rep.fail("`description` 含 XML/HTML 标签")
    if re.match(r"^(我|我们|you|I)\b", desc.strip()):
        rep.warn("`description` 建议用第三人称（当前以第一/第二人称开头）")


def check_skill_body(rep, body):
    if body is None:
        return
    n = len(body.splitlines())
    if n >= MAX_SKILL_BODY_LINES:
        rep.fail("SKILL.md 正文 %d 行，达到/超过 %d 行上限" % (n, MAX_SKILL_BODY_LINES))
    else:
        rep.ok("SKILL.md 正文 %d / %d 行" % (n, MAX_SKILL_BODY_LINES))


def check_references(rep, root):
    ref_dir = os.path.join(root, "references")
    if not os.path.isdir(ref_dir):
        rep.ok("无 references/ 目录，跳过该组检查")
        return
    names = sorted(f for f in os.listdir(ref_dir) if f.endswith(".md"))
    long_files = 0
    missing_toc = 0
    nested = 0
    for fname in names:
        text = read_text(os.path.join(ref_dir, fname))
        if len(text.splitlines()) > TOC_MIN_LINES:
            long_files += 1
            if "## 目录" not in text:
                missing_toc += 1
                rep.fail("references/%s 超过 %d 行，缺少 `## 目录`" % (fname, TOC_MIN_LINES))
        for other in names:
            if other != fname and other in text:
                nested += 1
                rep.fail("references/%s 引用了另一个 reference（`%s`）——引用不得嵌套"
                         % (fname, other))
    if long_files and not missing_toc:
        rep.ok("references/ 下 %d 个超长文件均带 `## 目录`" % long_files)
    if not nested:
        rep.ok("references/ 下 %d 个文件无嵌套引用" % len(names))


def check_links(rep, root):
    md_files = [p for p in iter_files(root, (".md",))]
    base = os.path.abspath(root)
    checked = 0
    for path in md_files:
        text = read_text(path)
        self_rel = rel(base, path)
        anchors = None
        in_fence = False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for target in re.findall(r"\]\(([^)\s]+)\)", line):
                if target.startswith("#"):
                    if anchors is None:
                        anchors = collect_anchors(text)
                    if target[1:] and target[1:] not in anchors:
                        rep.fail("%s：锚点 `%s` 无法解析" % (self_rel, target))
                    checked += 1
                    continue
                if re.match(r"^(https?:|mailto:)", target):
                    continue
                file_part, _, anchor = target.partition("#")
                if not file_part:
                    continue
                dest = os.path.normpath(os.path.join(os.path.dirname(path), file_part))
                if not os.path.exists(dest):
                    rep.fail("%s：链接目标不存在 `%s`" % (self_rel, target))
                    continue
                checked += 1
                if anchor and not os.path.isdir(dest):
                    if anchor not in collect_anchors(read_text(dest)):
                        rep.fail("%s：锚点 `%s` 在 %s 中无法解析"
                                 % (self_rel, target, file_part))
    rep.ok("相对链接/锚点检查：%d 处通过" % checked)


def check_line_endings(rep, root):
    bad = []
    for path in iter_files(root):
        with open(path, "rb") as fh:
            if b"\r\n" in fh.read():
                bad.append(rel(root, path))
    if bad:
        rep.fail("以下文件是 CRLF 行尾，应统一为 LF：%s" % bad[:8])
    else:
        rep.ok("全部文本文件行尾为 LF")


def check_scripts(rep, root):
    sdir = os.path.join(root, "scripts")
    if not os.path.isdir(sdir):
        return
    count = 0
    for fname in sorted(os.listdir(sdir)):
        if not fname.endswith(".py"):
            continue
        count += 1
        try:
            compile(read_text(os.path.join(sdir, fname)), fname, "exec")
        except SyntaxError as exc:
            rep.fail("scripts/%s 语法错误：%s" % (fname, exc))
    rep.ok("scripts/ 下 %d 个脚本编译通过" % count)


def check_version_consistency(rep, root, fm):
    meta = fm.get("metadata")
    if not isinstance(meta, dict) or "version" not in meta:
        rep.warn("frontmatter 里读不到 `metadata.version`，跳过版本一致性检查")
        return
    version = meta["version"]
    if not re.fullmatch(r"\d+\.\d+", version):
        rep.fail("`metadata.version` 应为 `主.次` 形态：%r" % version)
        return

    changelog = os.path.join(root, "CHANGELOG.md")
    if not os.path.isfile(changelog):
        rep.warn("没有 CHANGELOG.md，无法核对版本号")
    else:
        heads = re.findall(r"^##\s+(\d+\.\d+)\s*$", read_text(changelog), re.M)
        if not heads:
            rep.fail("CHANGELOG.md 里找不到 `## 主.次` 形态的小节标题")
        elif heads[0] != version:
            rep.fail("CHANGELOG.md 最新小节是 `## %s`，与 `metadata.version`（%s）不一致"
                     % (heads[0], version))
        else:
            rep.ok("版本一致：CHANGELOG 最新小节 == metadata.version == %s" % version)

    readme = os.path.join(root, "README.md")
    if os.path.isfile(readme):
        badges = set(re.findall(r"version-(\d+\.\d+)", read_text(readme)))
        if not badges:
            rep.warn("README.md 里没有 `version-主.次` 徽章，无法核对")
        elif badges - {version}:
            rep.fail("README.md 徽章版本 %s 与 `metadata.version`（%s）不一致"
                     % (sorted(badges), version))
        else:
            rep.ok("版本一致：README 徽章 == %s" % version)


def check_cache(rep, root):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "__pycache__":
            hits.append(rel(root, dirpath))
            continue
        for name in filenames:
            if name.endswith(CACHE_SUFFIXES):
                hits.append(rel(root, os.path.join(dirpath, name)))
    hits = sorted(set(hits))
    if hits:
        rep.fail("发现 Python 缓存残留（打包前必须清掉，否则会被带进发布物）：%s" % hits[:8])
    else:
        rep.ok("无 __pycache__ / *.pyc 残留")


def check_placeholders(rep, root):
    """清点发布前仍需处理的占位值。只报警不失败。"""
    total = 0
    files = []
    domains = []
    for path in iter_files(root, PORTABILITY_SUFFIXES):
        if os.path.basename(path) in PLACEHOLDER_SKIP_FILES:
            continue
        text = read_text(path)
        n = sum(text.count(t) for t in PLACEHOLDER_TOKENS)
        if n:
            total += n
            files.append("%s(%d)" % (rel(root, path), n))
        if PLACEHOLDER_DOMAIN_RE.search(text):
            domains.append(rel(root, path))
    if total:
        rep.warn("发现 %d 处模板占位符待替换：%s" % (total, "、".join(files)))
    else:
        rep.ok("未发现待替换的占位符")
    if domains:
        rep.warn("仍含占位域名（example.com 之类）：%s" % domains)


# --------------------------------------------------------------- 可移植性规则


def check_machine_paths(rep, root):
    """规则 12：文档与配置里不得出现执行环境专属的绝对路径。"""
    hits = []
    for path in iter_files(root, PORTABILITY_SUFFIXES):
        text = read_text(path)
        for i, line in enumerate(text.splitlines(), 1):
            if MACHINE_PATH_RE.search(line):
                hits.append("%s:%d" % (rel(root, path), i))
    if hits:
        rep.fail("发现硬编码的绝对路径（换一台机器即失效，请改用环境变量或占位符）：%s" % hits[:8])
    else:
        rep.ok("可移植性·无硬编码绝对路径")


def check_ip_literals(rep, root):
    """规则 13：不得把某个网络的实测地址写进包里。"""
    hits = []
    for path in iter_files(root):
        try:
            text = read_text(path)
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in IPV4_RE.finditer(line):
                ip = m.group(0)
                if ip.startswith(IPV4_ALLOWED_PREFIXES):
                    continue
                if ip.startswith(IPV4_ALLOWED_NETWORKS):
                    continue
                hits.append("%s:%d %s" % (rel(root, path), i, ip))
    if hits:
        rep.fail("发现 IP 字面量（某台机器/某个网络的实测值，换环境即失效）：%s" % hits[:8])
    else:
        rep.ok("可移植性·无白名单外的 IP 字面量")


def check_env_references(rep, root):
    """规则 14：文档里不得有无先行词的第一人称环境指代。豁免标记会被清点。"""
    hits = []
    exempts = []
    for path in iter_files(root, DOC_SUFFIXES):
        text = read_text(path)
        for i, line in enumerate(text.splitlines(), 1):
            word = next((w for w in ENV_REFERENCE_WORDS if w in line), None)
            if not word:
                continue
            if EXEMPT_MARKER in line:
                exempts.append("%s:%d" % (rel(root, path), i))
                continue
            hits.append("%s:%d「%s」" % (rel(root, path), i, word))
    if hits:
        rep.warn("发现第一人称环境指代（请逐条判断：该改，还是属于「话术示例 / 确指读者机器」"
                 "的合法例外；确实要举例说明被禁写法时加 `%s` 标记）：%s"
                 % (EXEMPT_MARKER, hits[:8]))
    else:
        rep.ok("可移植性·无未豁免的第一人称环境指代")
    if exempts:
        # 报成 PASS 而不是 WARN：这条豁免**是预期内的**（规则本身必须举出被禁写法），
        # 一条永远亮着的警告会训练人忽略警告。改为可见、可数的通过项 ——
        # 数量一旦上涨，就是有人在拿豁免绕过检查。
        rep.ok("可移植性·豁免标记 %d 处（有意保留，仅用于举例说明被禁写法）：%s"
               % (len(exempts), exempts[:8]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="selfcheck.py",
                                 description="技能包结构与可移植性自检（纯标准库）")
    ap.add_argument("root", nargs="?", default=".", help="技能仓库根目录（含 SKILL.md）")
    ap.add_argument("--quiet", action="store_true", help="只输出失败与警告")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("错误：%s 不是目录" % root, file=sys.stderr)
        return 2

    rep = Report()
    fm, body, _text = check_frontmatter(rep, root)
    if fm:
        check_name(rep, fm, root)
        check_description(rep, fm)
        check_version_consistency(rep, root, fm)
    check_skill_body(rep, body)
    check_references(rep, root)
    check_links(rep, root)
    check_line_endings(rep, root)
    check_scripts(rep, root)
    check_cache(rep, root)
    check_placeholders(rep, root)
    check_machine_paths(rep, root)
    check_ip_literals(rep, root)
    check_env_references(rep, root)

    if not args.quiet:
        for msg in rep.passes:
            print("  PASS  %s" % msg)
    for msg in rep.warns:
        print("  WARN  %s" % msg)
    for msg in rep.fails:
        print("  FAIL  %s" % msg)
    print("\n%d 通过 / %d 警告 / %d 失败" % (len(rep.passes), len(rep.warns), len(rep.fails)))
    if rep.fails:
        print("自检未通过")
        return 1
    print("自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
