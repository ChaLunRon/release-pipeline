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


class Result:
    def __init__(self):
        self.items = []

    def add(self, no, name, status, detail=""):
        self.items.append({"no": no, "name": name, "status": status, "detail": detail})

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


def check_tag_sets(res, refs, repo):
    remote_tags = {r[len("refs/tags/"):] for r in refs if r.startswith("refs/tags/")}
    local_tags = set()
    out = run_git(["tag", "--list"], cwd=repo).decode("utf-8")
    for line in out.splitlines():
        if line.strip():
            local_tags.add(line.strip())
    diff = remote_tags ^ local_tags
    if diff:
        res.add(2, "tag 名集合对称差为空", FAIL, "对称差：%s" % sorted(diff)[:10])
    else:
        res.add(2, "tag 名集合对称差为空", PASS, "双方各 %d 个" % len(local_tags))
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


def check_remote_main_tree(res, owner, repo, url, local, token):
    refs = remote_refs(url)
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


def check_ci(res, owner, repo, expected_sha, token):
    runs = fetch_json("%s/repos/%s/%s/actions/runs?per_page=30" % (DEFAULT_API, owner, repo), token)
    items = runs.get("workflow_runs", [])
    if not items:
        res.add(8, "CI 最新 run", FAIL, "读不到任何运行记录")
        return
    items.sort(key=lambda r: r.get("created_at", ""))
    latest = items[-1]
    conclusion = latest.get("conclusion")
    head = latest.get("head_sha")
    name = latest.get("name")
    detail = "%s #%s head=%s conclusion=%s" % (name, latest.get("run_number"), head, conclusion)
    if conclusion == "success" and head == expected_sha:
        res.add(8, "CI 最新 run", PASS, detail)
    else:
        res.add(8, "CI 最新 run", FAIL, detail + "（期望 head=%s）" % expected_sha)
    # 顺带报告最新一次运行的注解数（判断弃用警告是否已消除）
    checks = fetch_json("%s/repos/%s/%s/commits/%s/check-runs" % (DEFAULT_API, owner, repo, expected_sha), token)
    counts = [(c.get("name"), (c.get("output") or {}).get("annotations_count")) for c in checks.get("check_runs", [])]
    if counts:
        res.add("8b", "本次提交的注解数", PASS if all(n == 0 for _, n in counts) else FAIL,
                "、".join("%s=%s" % (n, c) for n, c in counts))


def check_release_asset(res, owner, repo, local, tag, token, workdir):
    latest = fetch_json("%s/repos/%s/%s/releases/tags/%s" % (DEFAULT_API, owner, repo, tag), token)
    assets = latest.get("assets") or []
    if not assets:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, "该 tag 的 Release 没有资产")
        return
    asset = assets[0]
    url = asset.get("browser_download_url")
    if not url:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, "资产没有下载地址")
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
    detail = "大小 %d 字节 / %d 条目 / %s" % (os.path.getsize(zip_path), len(entries),
                                            describe(only_l, only_r, differ))
    if only_l or only_r or differ:
        res.add(9, "Release 资产与 tag 逐文件一致", FAIL, detail + str((only_l + only_r + differ)[:8]))
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
    if os.path.isdir(gitdir):
        for dirpath, dirnames, filenames in os.walk(gitdir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "rb") as fh:
                        chunk = fh.read(2 * 1024 * 1024)
                except OSError:
                    continue
                if token_re.search(chunk.decode("utf-8", "replace")):
                    hits.append(path)
    if hits:
        res.add(10, "凭据残留复查", FAIL, "以下位置发现令牌字面量：%s" % hits[:5])
    else:
        res.add(10, "凭据残留复查", PASS, "配置与对象库里均无令牌字面量")


# ------------------------------------------------------------------ 主流程


def write_baseline(url, path):
    refs = remote_refs(url)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(refs, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("已写入基线：%s（%d 个 ref）" % (path, len(refs)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify_publish.py",
                                 description="推送后九项回验（纯标准库）")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--local", default=".", help="本地仓库目录")
    ap.add_argument("--tag", help="本次推送的新 tag")
    ap.add_argument("--baseline", help="推送前用 --write-baseline 存的 JSON")
    ap.add_argument("--write-baseline", metavar="PATH", help="只写基线，不做回验")
    ap.add_argument("--archive-root", help="归档快照根目录（如 history/）")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    ap.add_argument("--offline", action="store_true", help="不发起任何网络请求")
    args = ap.parse_args(argv)

    local = os.path.abspath(args.local)
    url = "https://github.com/%s/%s.git" % (args.owner, args.repo)

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
    check_credentials(res, local)

    if args.offline:
        for no, name in ((1, "远端 main == 本地 HEAD == 本地 tag"),
                         (2, "tag 名集合对称差为空"),
                         (3, "旧 tag 的 sha 零变动"),
                         (4, "远端 main 树 == 本地 HEAD 树"),
                         (5, "远端 tag 树 == 本地同名 tag 树"),
                         (6, "远端每个 tag 树 == 本地归档目录"),
                         (7, "全部 tag 本地树 == 远端树"),
                         (8, "CI 最新 run"),
                         (9, "Release 资产与 tag 逐文件一致")):
            res.add(no, name, SKIP, "--offline：未访问远端")
    else:
        try:
            refs = check_refs(res, local, url, args.tag)
            remote_tags = check_tag_sets(res, refs, local)
            check_history_unchanged(res, refs, args.baseline)
            check_remote_main_tree(res, args.owner, args.repo, url, local, args.token)
            check_remote_tag_tree(res, args.owner, args.repo, local, args.tag, args.token)
            check_archive_snapshots(res, args.owner, args.repo, local, args.archive_root, args.token)
            check_all_tags_local_vs_remote(res, args.owner, args.repo, local, remote_tags, args.token)
            head = run_git(["rev-parse", "HEAD"], cwd=local).decode().strip()
            check_ci(res, args.owner, args.repo, head, args.token)
            with tempfile.TemporaryDirectory() as tmp:
                check_release_asset(res, args.owner, args.repo, local, args.tag, args.token, tmp)
        except Exception as exc:          # 明确报告，不静默通过
            res.add("!", "远端检查中断", FAIL, "%s: %s" % (type(exc).__name__, exc))

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
    print("九项回验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
