#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推送后九项回验（纯标准库，无第三方依赖）。

    # 0) 推送**之前**先留一份基线，否则第 3 项无从比较
    python scripts/verify_publish.py --owner <owner> --repo <repo> --write-baseline tags.json

    # 1) 推送之后逐项回验
    python scripts/verify_publish.py --owner <owner> --repo <repo> \
        --local ./<技能目录> --tag 1.0 --baseline tags.json --archive-root ./history

设计要点
--------

**比对基准是 Git 对象指纹** ``sha1(b"blob <len>\\0" + data)``，不是工作区文件哈希、
也不是 zip 的哈希。它与平台、行尾、时区全部无关，也正是远端 API 返回值的口径。
把断言打在 zip 的整文件哈希上会得到一个**随机变红**的结果（zip 条目时间戳取构建机器
所在时区的时间）。

**「跳过」不等于「通过」。** 缺基线、缺权限、离线时相应项会被跳过，但一律**显式打印**
并单独计数；只要有一项被跳过，结论就不会写「全部通过」。任何软失败（异常被吞、
返回兜底值）都不算通过。

**先全部登记，再逐项改写。** 主流程会先把回验计划里的每一项登记为「未执行」，
跑完一项改写一项。这样一次远端异常（限流 / 403 / 超时）只会让后面的项显示为
**跳过**，而不是让它们从报告里**消失** —— 「N 通过 / 1 失败 / 0 跳过」而 N 小于总项数，
是比失败更危险的读数。

**令牌只从环境变量取**（`GH_TOKEN`），不提供命令行参数：命令行参数会进入 shell 历史
与进程列表，而「令牌不进 URL、不进 refspec、不被回显」是这套流程的核心纪律。

离线自测：`--offline` 下不会发起任何网络请求。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

DEFAULT_API = "https://api.github.com"
GIT_TIMEOUT = 120

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# 「CI 跑绿了」指的是**校验工作流**跑绿了，不能取「全部工作流里最新的一条」：
# 推 tag 之后发布工作流往往比校验更晚结束，于是「最新一条」通常是发布工作流 ——
# 它 conclusion=success、head_sha 也等于该 tag 的提交，判定「通过」，
# 但通过的是**发布工作流**。那是假阳性：通过得不对。
CI_WORKFLOW = "validate"

# 回验计划：**先把每一项登记为「未执行」，跑一项改写一项。**
# 原先的写法是「执行成功才登记」，于是第 4 项抛异常时第 5–9 项既不执行也不登记 ——
# 报告里它们**不存在**，汇总却打印「N 通过 / 1 失败 / 0 跳过」。
# 那直接违反了本技能自己的纪律「跳过必须显式打印并单独计数」。
CHECK_PLAN = (
    (1, "远端 main == 本地 HEAD == 本地 tag"),
    (2, "tag 名集合对称差为空"),
    (3, "旧 tag 的 sha 零变动"),
    (4, "远端 main 树 == 本地 HEAD 树"),
    (5, "远端 tag 树 == 本地同名 tag 树"),
    (6, "远端每个 tag 树 == 本地归档目录"),
    (7, "全部 tag 本地树 == 远端树"),
    (8, "CI 最新 run"),
    ("8b", "本次提交的注解数（附带判定）"),
    (9, "Release 资产与 tag 逐文件一致"),
    ("9b", "latest 落在最高版本（附带判定）"),
    (10, "凭据残留复查"),
)


class Result:
    def __init__(self):
        self.items = []

    def add(self, no, name, status, detail=""):
        """登记一项结果。**同号位覆盖**而不是追加 —— 这样主流程才能先把全部检查项
        预登记为「未执行」，再逐项改写。一次远端异常就不会让后面的项从报告里消失。"""
        item = {"no": no, "name": name, "status": status, "detail": detail}
        for i, old in enumerate(self.items):
            if old["no"] == no:
                self.items[i] = item
                return
        self.items.append(item)

    @property
    def fails(self):
        return [i for i in self.items if i["status"] == FAIL]

    @property
    def skips(self):
        return [i for i in self.items if i["status"] == SKIP]

    @property
    def passes(self):
        return [i for i in self.items if i["status"] == PASS]


# ------------------------------------------------------------------ 基础工具


def blob_sha(data):
    """Git 对象指纹，与 `git hash-object` 及远端 trees API 的 blob sha 同口径。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def run_git(args, cwd=None):
    proc = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def local_tree(repo, ref):
    """返回 {仓库内相对路径: blob sha}，取自本地对象库。"""
    out = run_git(["ls-tree", "-r", "-z", "--full-tree", ref], cwd=repo)
    result = {}
    for entry in out.split(b"\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        result[path.decode("utf-8")] = fields[2].decode("ascii")
    return result


def resolve_tag_commit(repo, tag):
    """把 tag 解析到它指向的 commit —— annotated tag 的 objectname 是 tag 对象，
    直接拿去查远端树会查错。"""
    return run_git(["rev-parse", "%s^{commit}" % tag], cwd=repo).decode().strip()


def remote_refs(url):
    """解析 `git ls-remote` 输出为 {ref: sha}。公开仓库不需要认证。"""
    out = run_git(["ls-remote", url])
    refs = {}
    for line in out.decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            refs[parts[1].strip()] = parts[0].strip()
    return refs


# ------------------------------------------------------------------ HTTP 层

def fetch_json(url, token=None):
    """单点网络出口：测试通过替换本模块的同名属性来桩掉它（离线可跑）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "verify-publish", "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bytes(url, token=None):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "verify-publish"})
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as resp:
        return resp.read()


def api_tree(owner, repo, sha, token=None):
    """递归取树 -> {路径: blob sha}（只取 blob 条目）。"""
    data = fetch_json("%s/repos/%s/%s/git/trees/%s?recursive=1" % (DEFAULT_API, owner, repo, sha), token)
    return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") == "blob"}


# -------------------------------------------------------------- 目录比对工具


def compare_maps(left, right):
    """返回 (左侧独有, 右侧独有, 同名但内容不同) 三栏。**永远分栏报告** ——
    第二栏为空时就能一眼判定是路径前缀写错了，而不是内容真的不一致。"""
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    differ = sorted(p for p in set(left) & set(right) if left[p] != right[p])
    return only_left, only_right, differ


def describe(only_left, only_right, differ):
    return "左独有 %d / 右独有 %d / 内容不同 %d" % (len(only_left), len(only_right), len(differ))


def dir_blobs(root):
    """把一个目录读成 {相对路径: blob sha}，不依赖该目录里有没有 .git。"""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as fh:
                result[rel] = blob_sha(fh.read())
    return result


# ------------------------------------------------------------------ 十项检查


def check_refs(res, repo, url, tag):
    refs = remote_refs(url)
    remote_main = refs.get("refs/heads/main") or refs.get("refs/heads/master")
    local_head = run_git(["rev-parse", "HEAD"], cwd=repo).decode().strip()
    local_tag = resolve_tag_commit(repo, tag)
    detail = "remote=%s local HEAD=%s local tag=%s" % (remote_main, local_head, local_tag)
    if remote_main and remote_main == local_head == local_tag:
        res.add(1, "远端 main == 本地 HEAD == 本地 tag", PASS, detail)
    else:
        res.add(1, "远端 main == 本地 HEAD == 本地 tag", FAIL, detail)
    return refs


def local_tags(repo):
    out = run_git(["tag", "--list"], cwd=repo).decode("utf-8")
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_tag_sets(res, refs, repo):
    remote_tags = {r[len("refs/tags/"):] for r in refs if r.startswith("refs/tags/")}
    local = set(local_tags(repo))
    diff = remote_tags ^ local
    if diff:
        res.add(2, "tag 名集合对称差为空", FAIL, "对称差：%s" % sorted(diff)[:10])
    else:
        res.add(2, "tag 名集合对称差为空", PASS, "双方各 %d 个" % len(local))
    return remote_tags


def check_history_unchanged(res, refs, baseline_path):
    if not baseline_path:
        res.add(3, "旧 tag 的 sha 零变动", SKIP,
                "未提供推送前基线（用 --write-baseline 先存一份），无法判定")
        return
    if not os.path.isfile(baseline_path):
        res.add(3, "旧 tag 的 sha 零变动", SKIP, "基线文件不存在：%s" % baseline_path)
        return
    with open(baseline_path, "r", encoding="utf-8") as fh:
        baseline = json.load(fh)
    changed = []
    for ref, sha in baseline.items():
        if refs.get(ref) != sha:
            changed.append("%s: %s -> %s" % (ref, sha, refs.get(ref)))
    if changed:
        res.add(3, "旧 tag 的 sha 零变动", FAIL,
                "历史被动过：%s" % changed[:5])
    else:
        res.add(3, "旧 tag 的 sha 零变动", PASS, "基线里 %d 个 ref 全部未变" % len(baseline))


def check_remote_main_tree(res, owner, repo, refs, local, token):
    # refs 由调用方传入，**不再自己重取一遍**：原先一次回验里 remote_refs 被调用三次，
    # 每次都是独立的网络往返，也各自是独立的失败点。
    remote_main = refs.get("refs/heads/main") or refs.get("refs/heads/master")
    if not remote_main:
        res.add(4, "远端 main 树 == 本地 HEAD 树", FAIL, "远端读不到 main 的 sha")
        return
    remote = api_tree(owner, repo, remote_main, token)
    local = local_tree(local, "HEAD")
    only_l, only_r, differ = compare_maps(local, remote)
    if only_l or only_r or differ:
        res.add(4, "远端 main 树 == 本地 HEAD 树", FAIL,
                "%s（左=本地）%s" % (describe(only_l, only_r, differ),
                                    (only_l + only_r + differ)[:8]))
    else:
        res.add(4, "远端 main 树 == 本地 HEAD 树", PASS, "%d 个文件逐一致" % len(local))


def check_remote_tag_tree(res, owner, repo, local, tag, token):
    remote_sha = run_git(["rev-parse", "%s^{commit}" % tag], cwd=local).decode().strip()
    remote = api_tree(owner, repo, remote_sha, token)
    local = local_tree(local, tag)
    only_l, only_r, differ = compare_maps(local, remote)
    if only_l or only_r or differ:
        res.add(5, "远端 tag 树 == 本地同名 tag 树", FAIL,
                "%s%s" % (describe(only_l, only_r, differ), (only_l + only_r + differ)[:8]))
    else:
        res.add(5, "远端 tag 树 == 本地同名 tag 树", PASS, "%s：%d 个文件逐一致" % (tag, len(local)))


def check_archive_snapshots(res, owner, repo, local, archive_root, token):
    if not archive_root:
        res.add(6, "远端每个 tag 树 == 本地归档目录", SKIP, "未提供 --archive-root")
        return
    if not os.path.isdir(archive_root):
        res.add(6, "远端每个 tag 树 == 本地归档目录", SKIP,
                "归档目录不存在：%s" % archive_root)
        return
    bad = []
    checked = 0
    for name in sorted(os.listdir(archive_root)):
        snap = os.path.join(archive_root, name)
        if not os.path.isdir(snap):
            continue
        ref = run_git(["rev-parse", "%s^{commit}" % name], cwd=local)
        remote = api_tree(owner, repo, ref.decode().strip(), token)
        localfiles = dir_blobs(snap)
        # 归档目录常是嵌套两层：history/<tag>/<name>-<主>-<次>/
        if len(localfiles) == 0 or not (set(localfiles) & set(remote)):
            inner = [os.path.join(snap, d) for d in os.listdir(snap)]
            inner = [d for d in inner if os.path.isdir(d)]
            if len(inner) == 1:
                localfiles = dir_blobs(inner[0])
        only_l, only_r, differ = compare_maps(localfiles, remote)
        checked += 1
        if only_l or only_r or differ:
            bad.append("%s：%s" % (name, describe(only_l, only_r, differ)))
    if bad:
        res.add(6, "远端每个 tag 树 == 本地归档目录", FAIL, str(bad[:5]))
    else:
        res.add(6, "远端每个 tag 树 == 本地归档目录", PASS, "%d 个归档逐一一致" % checked)


def check_all_tags_local_vs_remote(res, owner, repo, local, remote_tags, token):
    bad = []
    for name in sorted(remote_tags):
        try:
            ref = resolve_tag_commit(local, name)
        except RuntimeError:
            bad.append("%s：本地没有这个 tag" % name)
            continue
        remote = api_tree(owner, repo, ref, token)
        left = local_tree(local, name)
        only_l, only_r, differ = compare_maps(left, remote)
        if only_l or only_r or differ:
            bad.append("%s：%s" % (name, describe(only_l, only_r, differ)))
    if bad:
        res.add(7, "全部 tag 本地树 == 远端树", FAIL, str(bad[:5]))
    else:
        res.add(7, "全部 tag 本地树 == 远端树", PASS, "%d 个 tag 逐一一致" % len(remote_tags))


def _is_workflow(run, workflow):
    """按工作流名或工作流文件名匹配。**必须过滤** —— 见文件头 CI_WORKFLOW 的说明。"""
    if run.get("name") == workflow:
        return True
    path = run.get("path") or ""
    return path.rsplit("/", 1)[-1] in (workflow, workflow + ".yml")


def check_ci(res, owner, repo, expected_sha, token, workflow=CI_WORKFLOW):
    runs = fetch_json("%s/repos/%s/%s/actions/runs?per_page=100" % (DEFAULT_API, owner, repo), token)
    items = runs.get("workflow_runs", [])
    if not items:
        res.add(8, "CI 最新 run", SKIP, "读不到任何运行记录（无法判定，不等于通过）")
        return
    scoped = [r for r in items if _is_workflow(r, workflow)]
    if not scoped:
        res.add(8, "CI 最新 run", SKIP,
                "最近 %d 次运行里没有 `%s` 工作流的记录。推 tag 之后发布工作流通常比校验"
                "更晚结束，不按名字过滤就会拿它顶替校验 —— 那是假阳性。"
                % (len(items), workflow))
        return
    scoped.sort(key=lambda r: r.get("created_at", ""))
    latest = scoped[-1]
    conclusion = latest.get("conclusion")
    head = latest.get("head_sha")
    name = latest.get("name")
    detail = "%s #%s head=%s conclusion=%s" % (name, latest.get("run_number"), head, conclusion)
    if conclusion == "success" and head == expected_sha:
        res.add(8, "CI 最新 run", PASS, detail)
    else:
        res.add(8, "CI 最新 run", FAIL, detail + "（期望 head=%s）" % expected_sha)
    # 顺带报告本次提交的注解数（判断弃用警告是否已消除）
    checks = fetch_json("%s/repos/%s/%s/commits/%s/check-runs" % (DEFAULT_API, owner, repo, expected_sha), token)
    counts = [(c.get("name"), (c.get("output") or {}).get("annotations_count")) for c in checks.get("check_runs", [])]
    if counts:
        res.add("8b", "本次提交的注解数（附带判定）",
                PASS if all(n == 0 for _, n in counts) else FAIL,
                "、".join("%s=%s" % (n, c) for n, c in counts))
    else:
        res.add("8b", "本次提交的注解数（附带判定）", SKIP, "该提交没有 check-run 记录")


def expected_asset_name(local):
    """发布资产的命名约定：`<包名>.zip`，包名 = `SKILL.md` 的 `name` = 技能目录名。

    命名三条一致是本技能的硬要求，所以这里有唯一确定的期望值 ——
    这正好让「按名字匹配」成为可能，而不必去赌资产列表的顺序。
    """
    return os.path.basename(os.path.abspath(local)) + ".zip"


def check_latest_release(res, owner, repo, local, token):
    """`latest` 必须落在最高版本 tag 上。回填历史 Release 时最容易弄错的就是这一处 ——
    发布工作流本身也要靠 `--latest=false` 才守得住，所以值得单独回验一次。"""
    version_tags = [t for t in local_tags(local) if re.fullmatch(r"\d+\.\d+", t)]
    if not version_tags:
        res.add("9b", "latest 落在最高版本（附带判定）", SKIP, "本地没有 `主.次` 形态的 tag")
        return
    highest = max(version_tags, key=lambda t: tuple(int(x) for x in t.split(".")))
    data = fetch_json("%s/repos/%s/%s/releases/latest" % (DEFAULT_API, owner, repo), token)
    got = data.get("tag_name")
    if got == highest:
        res.add("9b", "latest 落在最高版本（附带判定）", PASS,
                "latest=%s（本地最高版本 %s）" % (got, highest))
    else:
        res.add("9b", "latest 落在最高版本（附带判定）", FAIL,
                "latest=%s，但本地最高版本是 %s" % (got, highest))


def check_release_asset(res, owner, repo, local, tag, token, workdir, asset_name=None):
    want = asset_name or expected_asset_name(local)
    rel = fetch_json("%s/repos/%s/%s/releases/tags/%s" % (DEFAULT_API, owner, repo, tag), token)

    # 状态与资产在同一个响应里，不该只看资产。
    state = [k for k in ("draft", "prerelease") if rel.get(k)]

    assets = rel.get("assets") or []
    if not assets:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, "该 tag 的 Release 没有资产")
        return

    # **按期望文件名挑，不取列表里的第一个。** 一个 Release 常同时挂
    # zip + 校验和 + 签名 + SBOM，取第一个等于在赌列表顺序。
    names = [a.get("name") for a in assets]
    asset = next((a for a in assets if a.get("name") == want), None)
    if asset is None:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL,
                "没有名为 `%s` 的资产（实际有：%s）。按名字匹配而不是取第一个 —— "
                "取第一个等于在赌列表顺序。" % (want, names[:6]))
        return

    url = asset.get("browser_download_url")
    if not url:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, "资产 `%s` 没有下载地址" % want)
        return
    zip_path = os.path.join(workdir, os.path.basename(url) or "asset.zip")
    with open(zip_path, "wb") as fh:
        fh.write(fetch_bytes(url, token))
    expected = local_tree(local, tag)
    with zipfile.ZipFile(zip_path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
    tops = {n.split("/")[0] for n in entries}
    if len(tops) != 1:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, "zip 顶层目录不唯一：%s" % sorted(tops))
        return
    got = {n.split("/", 1)[1]: blob_sha(d) for n, d in entries.items()}
    only_l, only_r, differ = compare_maps(expected, got)
    detail = "资产 %s / 大小 %d 字节 / %d 条目 / %s" % (
        want, os.path.getsize(zip_path), len(entries), describe(only_l, only_r, differ))
    if only_l or only_r or differ:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, detail + str((only_l + only_r + differ)[:8]))
    elif state:
        # 发布出去就是给人用的。标成草稿或预发布却当成正式发布来「回验通过」，是自欺。
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL,
                detail + "；但该 Release 状态为 %s —— 对外并不可用" % "/".join(state))
    else:
        res.add(9, "Release 资产与 tag 逐文件一致", PASS, detail)


# 元规则（与本仓库 selfcheck.py 一致）：**检查某种字面量的代码，自身不得含有该字面量的
# 完整形态。** 令牌前缀一律用字符串拼接构造 —— 否则
#   ① 平台的密钥扫描器会把这段源码当成真凭据命中；
#   ② 将来一次全局替换（换域名、换前缀）会把这条规则本身一起改掉，
#      而检查器会**静默失效**：它照样打印「通过」，只是再也查不出任何东西。
_TOKEN_PREFIXES = ("ghp" + "_", "github" + "_pat" + "_")
_TOKEN_RE = re.compile("|".join(re.escape(p) + r"[A-Za-z0-9_]{20,}"
                               for p in _TOKEN_PREFIXES))


# 对象库文件**分块读全**，且块间带重叠。
# 原先只读每个文件的前 2 MiB，而 `.git` 下的 packfile 很容易超过这个值 ——
# 落在其后的令牌查不到，结论却照样报 PASS。那是典型的**软失败**：
# 截断扫描的结果被当成了完整结论，正好违反本文件第六节自己的纪律。
# 重叠 128 字节，防止一个令牌恰好横跨分块边界而被漏掉（令牌前缀 + 至少 20 个字符）。
CHUNK_BYTES = 1 << 20
CHUNK_OVERLAP = 128


def _stream_contains_token(path, token_re):
    """True / False / None（None = 读不出来，**不等于没有**）。"""
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(CHUNK_BYTES)
                if not chunk:
                    return False
                buf = tail + chunk
                if token_re.search(buf.decode("utf-8", "replace")):
                    return True
                tail = buf[-CHUNK_OVERLAP:]
    except OSError:
        return None


def check_credentials(res, repo):
    token_re = _TOKEN_RE
    hits = []
    for path in (os.path.join(repo, ".git", "config"),
                 os.path.expanduser(os.path.join("~", ".gitconfig")),
                 os.path.expanduser(os.path.join("~", ".git-credentials"))):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    if token_re.search(fh.read()):
                        hits.append(path)
            except OSError:
                pass
    gitdir = os.path.join(repo, ".git")
    scanned = 0
    unreadable = []
    if os.path.isdir(gitdir):
        for dirpath, dirnames, filenames in os.walk(gitdir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                scanned += 1
                found = _stream_contains_token(path, token_re)
                if found is None:
                    unreadable.append(os.path.relpath(path, gitdir))
                elif found:
                    hits.append(path)
    if hits:
        res.add(10, "凭据残留复查", FAIL, "以下位置发现令牌字面量：%s" % hits[:5])
    elif unreadable:
        # 读不到就是读不到 —— 不能在覆盖不全的情况下宣称「没有」。
        res.add(10, "凭据残留复查", SKIP,
                "对象库里有 %d 个文件读不出来，**无法断言「没有令牌」**：%s"
                % (len(unreadable), unreadable[:3]))
    else:
        res.add(10, "凭据残留复查", PASS,
                "配置与对象库**逐文件读全**（对象库 %d 个文件），均无令牌字面量" % scanned)


# ------------------------------------------------------------------ 主流程


def write_baseline(url, path):
    refs = remote_refs(url)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(refs, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("已写入基线：%s（%d 个 ref）" % (path, len(refs)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify_publish.py",
                                 description="推送后回验（纯标准库）")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--local", default=".", help="本地仓库目录")
    ap.add_argument("--tag", help="本次推送的新 tag")
    ap.add_argument("--baseline", help="推送前用 --write-baseline 存的 JSON")
    ap.add_argument("--write-baseline", metavar="PATH", help="只写基线，不做回验")
    ap.add_argument("--archive-root", help="归档快照根目录（如 history/）")
    ap.add_argument("--asset-name", help="Release 资产名（默认 `<包名>.zip`，包名取本地目录名）")
    ap.add_argument("--ci-workflow", default=CI_WORKFLOW,
                    help="用哪个工作流判定「CI 跑绿了」（默认 %s）" % CI_WORKFLOW)
    ap.add_argument("--offline", action="store_true", help="不发起任何网络请求")
    args = ap.parse_args(argv)

    local = os.path.abspath(args.local)
    url = "https://github.com/%s/%s.git" % (args.owner, args.repo)
    # 令牌只从环境变量取，**不开命令行口**：命令行参数会进入 shell 历史与进程列表，
    # 而「令牌不进 URL、不进 refspec、不被回显」是阶段 5 的核心纪律。
    token = os.environ.get("GH_TOKEN")

    if args.write_baseline:
        write_baseline(url, args.write_baseline)
        return 0

    if not args.tag:
        print("错误：回验模式必须给 --tag", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(local, ".git")):
        print("错误：%s 不是 git 仓库" % local, file=sys.stderr)
        return 2

    res = Result()
    # **先全部登记为「未执行」，再逐项改写。** 任何中断都表现为「跳过」而不是「消失」。
    for no, name in CHECK_PLAN:
        skip_reason = ("--offline：未访问远端" if args.offline and no != 10 else "未执行")
        res.add(no, name, SKIP, skip_reason)

    check_credentials(res, local)

    if not args.offline:
        try:
            refs = check_refs(res, local, url, args.tag)
            remote_tags = check_tag_sets(res, refs, local)
            check_history_unchanged(res, refs, args.baseline)
            check_remote_main_tree(res, args.owner, args.repo, refs, local, token)
            check_remote_tag_tree(res, args.owner, args.repo, local, args.tag, token)
            check_archive_snapshots(res, args.owner, args.repo, local, args.archive_root, token)
            check_all_tags_local_vs_remote(res, args.owner, args.repo, local, remote_tags, token)
            head = run_git(["rev-parse", "HEAD"], cwd=local).decode().strip()
            check_ci(res, args.owner, args.repo, head, token, args.ci_workflow)
            check_latest_release(res, args.owner, args.repo, local, token)
            with tempfile.TemporaryDirectory() as tmp:
                check_release_asset(res, args.owner, args.repo, local, args.tag, token, tmp,
                                    args.asset_name)
        except Exception as exc:          # 明确报告，不静默通过
            res.add("!", "远端检查中断", FAIL,
                    "%s: %s（中断之后的项保持「未执行」—— 既不是通过，也不是失败）"
                    % (type(exc).__name__, exc))

    for item in sorted(res.items, key=lambda i: str(i["no"])):
        print("[%s] %s %s%s" % (item["status"], item["no"], item["name"],
                                ("  —— " + item["detail"]) if item["detail"] else ""))
    print("\n%d 通过 / %d 失败 / %d 跳过"
          % (len(res.passes), len(res.fails), len(res.skips)))
    if res.fails:
        print("回验未通过")
        return 1
    if res.skips:
        print("回验未发现失败，但有 %d 项被跳过 —— 这**不等于**全部通过" % len(res.skips))
        return 0
    print("回验全部通过（%d 项）" % len(res.passes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
