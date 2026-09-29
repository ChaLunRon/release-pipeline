#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推送后九项回验 + 四项附带核验（纯标准库，无第三方依赖）。

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

**「进行中」是第三种状态，不是失败。** CI 的 `conclusion` 在运行结束前是 `None` ——
刚推完就立刻回验，**必然**撞上它（实测：`validate #5 conclusion=None`，而同一次里
「资产与 tag 一致」「latest 落在最高版本」两项已 PASS，说明流水线只是还没跑完）。
把它判成 FAIL 会让人以为线上坏了，于是它单列成 `PENDING`：不计入失败，也不算通过，
措辞里写明「稍后重跑即可」。

**编号约定**：`1`–`9` 是核心九项；`8b` / `9b` / `10` / `10b` 是**附带核验**
（编号带字母后缀，或排在九项之后）。报告按数字排序，不按字符串排序 ——
否则「第 10 项」会插在「第 1 项」后面。

**先全部登记，再逐项改写。** 主流程会先把回验计划里的每一项登记为「未执行」，
跑完一项改写一项。这样一次远端异常（限流 / 403 / 超时）只会让后面的项显示为
**跳过**，而不是让它们从报告里**消失** —— 「N 通过 / 1 失败 / 0 跳过」而 N 小于总项数，
是比失败更危险的读数。

**「本次提交」是第 8 / 8b 两项的取数口径。** 第 8 项按 `head_sha` 取本次提交的运行记录，
第 8b 项只统计平台自家 CI 产生的 check run。少这两层限定，仓库里一旦有机器人开 PR，
就会得到两个**永久失败**的假阳性 —— 而永远亮着的红叉会训练人忽略红叉。

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

PASS, FAIL, SKIP, PENDING = "PASS", "FAIL", "SKIP", "PENDING"

# 「CI 跑绿了」指的是**校验工作流**跑绿了，不能取「全部工作流里最新的一条」：
# 推 tag 之后发布工作流往往比校验更晚结束，于是「最新一条」通常是发布工作流 ——
# 它 conclusion=success、head_sha 也等于该 tag 的提交，判定「通过」，
# 但通过的是**发布工作流**。那是假阳性：通过得不对。
CI_WORKFLOW = "validate"

# 注解只统计**本次提交自己的 CI** 产生的 check run。
#
# ⚠️ 不能用「app 是不是 github-actions」来圈定 —— 2026-09-29 实测被推翻：
# 平台给依赖更新机器人建的 check run 名字叫 `Dependabot`、**app slug 同样是
# `github-actions`**，上面还挂着一条平台通知（`ubuntu-latest` 迁移公告）。
# 按 app 过滤会把它算进来，报出一个把别人的话记到自己头上的假 FAIL。
#
# 可靠的口径是**归属**：check run 的 `details_url` 里带着它所属的
# `/actions/runs/<run_id>/job/<job_id>`，拿本次提交的 `CI_WORKFLOW` 那几个 run 的 id 去比。
# 这条对**任何**往仓库里挂检查的第三方 App 都成立，与它叫什么名字无关。

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
    (8, "CI 本次提交的 run"),
    ("8b", "本次提交的注解数（只算 Actions，附带判定）"),
    (9, "Release 资产与 tag 逐文件一致"),
    ("9b", "latest 落在最高版本（附带判定）"),
    (10, "凭据残留复查"),
    ("10b", "平台安全设置（只读核验，附带判定）"),
)

CHECK_TITLES = dict((no, name) for no, name in CHECK_PLAN)


def report_order(no):
    """报告里的排序键：**先按数字、再按后缀**（`8` < `8b` < `9` < `10` < `10b`），
    没有编号的（如中断标记 `!`）排在最前。

    原先直接用 `str(no)` 排 ⇒ 「第 10 项」会插在「第 1 项」后面。报告是给人读的，
    顺序错乱会让人以为有重复或漏项 —— 而这恰恰是这份报告最不该出现的效果。
    """
    m = re.match(r"^(\d+)(.*)$", str(no))
    if not m:
        return (0, 0, str(no))
    return (1, int(m.group(1)), m.group(2))


def _guard(res, item, fn, *a, **kw):
    """**逐项隔离**：某一项自己抛异常，不许让后面的项一起消失。

    原先只有一个包住整段远端检查的 `try`：第 6 项因为「归档目录名不是本仓库的 tag」
    抛了 `RuntimeError`，第 7–9b 项就连跑都没跑（报告里全是「未执行」）。
    而第 9 项恰恰是「Release 资产与 tag 是否一致」—— **真问题最可能就藏在被带崩的那几项里**。

    所以：异常算作**本项的失败**（并且明说「这是回验工具的问题，不代表线上坏了」），
    其余各项照跑。
    """
    try:
        fn(*a, **kw)
    except Exception as exc:
        res.add(item, CHECK_TITLES[item], FAIL,
                "该项自身抛出异常（%s: %s）—— 这是**回验工具**没能完成检查，"
                "既不等于线上有问题，也不等于通过；其余各项仍会照跑"
                % (type(exc).__name__, exc))


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

    @property
    def pending(self):
        """「进行中」既不是失败也不是通过 —— 结论行必须把它单列出来。"""
        return [i for i in self.items if i["status"] == PENDING]


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

_CONNECT_HOST = None


def force_connect_host(host):
    """把**所有** DNS 解析都指向 `host` 再连（端口不变，TLS 的 SNI 仍是真实域名）。

    为什么需要它：有些环境把 GitHub 域名写进 `hosts` 指到本机，由一个本地加速器
    **按 SNI 转发**。这时 `api.github.com` 是通的，但第 9 项要下载的 Release 资产会
    302 到一个**不在 hosts 里的域名**（`objects.githubusercontent.com` 一类），
    那个域名解析不出来 ⇒ 第 9 项「资产与 tag 是否一致」直接做不了。
    实测（2026-09-29，本机加速器开着）：不设它即 `getaddrinfo failed`，
    设成 `127.0.0.1` 后同样的 URL 取到 157 315 字节、sha256 与平台 digest 一致。

    因为只是把**解析**改掉，真实域名仍进 SNI，证书校验与主机名绑定都不受影响 ——
    这不是「跳过安全检查」，只是换一个能连通的出口地址。
    """
    global _CONNECT_HOST
    _CONNECT_HOST = host
    import socket
    orig = socket.getaddrinfo

    def patched(h, port, *a, **kw):
        return orig(host, port, *a, **kw)

    socket.getaddrinfo = patched


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


def strip_peel(name):
    """去掉 annotated tag 在 `ls-remote` 里的**解引用后缀**。

    带注释的 tag 在 `ls-remote` 输出里**成对出现**：`X` 与 `X` 加一个 `^{}` 尾巴
    （后者是它解引用到的对象），而本地 `git tag --list` 只列干净名字。
    不剥这个后缀，集合比较会**每次**都报「有差异」，差异数恰好等于 tag 个数 ——
    那是本工具的假阳性，不是线上少了东西。
    """
    suffix = "^" + "{}"
    return name[:-len(suffix)] if name.endswith(suffix) else name


def check_tag_sets(res, refs, repo):
    remote_tags = {strip_peel(r[len("refs/tags/"):]) for r in refs if r.startswith("refs/tags/")}
    local = set(local_tags(repo))
    diff = remote_tags ^ local
    if diff:
        res.add(2, "tag 名集合对称差为空", FAIL, "对称差：%s" % sorted(diff)[:10])
    else:
        res.add(2, "tag 名集合对称差为空", PASS, "双方各 %d 个" % len(local))
    return remote_tags


def check_history_unchanged(res, refs, baseline_path, tag=None):
    """第 3 项：**只比 tag**。

    原先拿基线里的**全部 ref** 与现在比，于是每次推送都必然报「历史被动过」——
    因为 `HEAD` 与 `refs/heads/main` 正是被这次推送移动的。若期间还有人
    **关掉一个机器人开的 PR**，`refs/heads/dependabot/*` 与 `refs/pull/*` 会被一并删除，
    差异数又凭空多几条。这些变化全都是**预期内**的，与「旧 tag 有没有被重指」无关。
    实测（2026-09-29，release-pipeline 推 4.0）差异 5 处，**没有一处是 tag**。

    本条要守的是：**已经发布出去的 tag，内容不许被就地改写**。
    所以口径收窄成 `refs/tags/*`，并**排除本次新推的那个 tag**（它本来就该是新的）。
    """
    if not baseline_path:
        res.add(3, "旧 tag 的 sha 零变动", SKIP,
                "未提供推送前基线（用 --write-baseline 先存一份），无法判定")
        return
    if not os.path.isfile(baseline_path):
        res.add(3, "旧 tag 的 sha 零变动", SKIP, "基线文件不存在：%s" % baseline_path)
        return
    with open(baseline_path, "r", encoding="utf-8") as fh:
        baseline = json.load(fh)

    base_tags = {k: v for k, v in baseline.items() if k.startswith("refs/tags/")}
    skipped = len(baseline) - len(base_tags)
    note = ""
    if skipped:
        note = ("；另有 %d 个非 tag 引用（`HEAD` / 分支 / PR）**不计入** —— "
                "它们本来就会随推送与 PR 的开关而变，与本项无关" % skipped)
    if tag:
        mine = "refs/tags/%s" % tag
        dropped = [k for k in base_tags if k.startswith(mine)]
        base_tags = {k: v for k, v in base_tags.items() if not k.startswith(mine)}
        if dropped:
            note += "；本次新推的 `%s` 已排除" % tag
    if not base_tags:
        res.add(3, "旧 tag 的 sha 零变动", SKIP,
                "基线里除了本次新推的 tag 没有别的 tag，无从判定「旧 tag 是否被改」" + note)
        return

    changed = []
    for ref, sha in base_tags.items():
        if refs.get(ref) != sha:
            changed.append("%s: %s -> %s" % (ref, sha, refs.get(ref)))
    if changed:
        res.add(3, "旧 tag 的 sha 零变动", FAIL,
                "历史被动过：%s" % changed[:5])
    else:
        res.add(3, "旧 tag 的 sha 零变动", PASS,
                "基线里 %d 个旧 tag 引用全部未变%s" % (len(base_tags), note))


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


def _zip_blobs(path):
    """zip -> {相对路径: blob sha}（剥掉唯一的顶层目录）。"""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        tops = {n.split("/")[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)
        return {n.split("/", 1)[1] if strip else n: blob_sha(zf.read(n)) for n in names}


def archive_snapshot_files(snap, remote=None):
    """归档目录里冻结的「一份内容」有三种常见形态，都要认：

    ① 解出来的快照目录（`history/1.0/<包名>/`）—— 早期归档连 `.git` 一起存的形态；
    ② 目录本身就是快照（`history/2.0/<若干源码文件>`）；
    ③ 只放了打包产物（`history/2.0/<包名>.zip`）—— 后期归档的形态。

    ⚠️ **形态按内容判定，不按形状猜，而且要用「完全匹配数」打分选。**
    两条都是踩出来的：

    ① 只按形状（如「里面只有一个子目录就是它」）会把快照里**普通的子目录**
       （`docs/`、`scripts/`）当成那一整份内容 —— 比的是别人的一小部分。
    ② 只看「路径有没有交集」也不够：`history/2.0/` 里同时放着**交付文档**和打包产物，
       而交付文档里的 `README.md` 与仓库里的 `README.md` **恰好同名** ⇒
       交集非空，于是选中了交付文档，报出「左独有 4 / 右独有 23」这种看着很严重的差异
       （实测 2026-09-29）。
       所以按 **(内容完全相同的文件数, 路径重合数)** 打分取最高 —— 打包产物会以
       24/24 完胜交付文档的 0/1。

    返回 (文件表, 形态说明)。**形态说明要进报告** —— 否则「比的是什么」无从复核。
    """
    inner = [os.path.join(snap, d) for d in sorted(os.listdir(snap))
             if os.path.isdir(os.path.join(snap, d))]
    zips = [f for f in sorted(os.listdir(snap)) if f.lower().endswith(".zip")]
    candidates = [(dir_blobs(snap), "目录内文件")]
    if len(inner) == 1:
        candidates.append((dir_blobs(inner[0]),
                           "解出的快照目录 %s/" % os.path.basename(inner[0])))
    if len(zips) == 1:
        candidates.append((_zip_blobs(os.path.join(snap, zips[0])),
                           "打包产物 %s" % zips[0]))
    if remote:
        best = None
        for files, form in candidates:
            exact = sum(1 for k, v in files.items() if remote.get(k) == v)
            overlap = len(set(files) & set(remote))
            score = (exact, overlap)
            if best is None or score > best[0]:
                best = (score, files, form)
        if best and (best[0][0] or best[0][1]):
            return best[1], best[2]
    return candidates[0]


def check_archive_snapshots(res, owner, repo, local, archive_root, token):
    """第 6 项：归档快照 vs 远端同名 tag 树。

    ⚠️ **归档目录名未必是本仓库的 tag。** 本项目 `history/1.0/` 存的是**前身项目**的
    快照，`1.0` 这个 tag 在**本仓库里根本不存在** ⇒ 原先直接 `git rev-parse 1.0^{commit}`
    抛异常，把整个远端段（第 6–9b 项）一起带崩，报告里它们全是「未执行」。
    实测（2026-09-29）：一次回验因此只跑完 4 项就中断，而**真正的线上问题恰恰可能在
    后面那几项里** —— 例如第 9 项「Release 资产与 tag 是否一致」。
    所以这里改成：**没有同名 tag 就跳过这一条并写明原因，继续下一条。**
    """
    if not archive_root:
        res.add(6, "远端每个 tag 树 == 本地归档目录", SKIP, "未提供 --archive-root")
        return
    if not os.path.isdir(archive_root):
        res.add(6, "远端每个 tag 树 == 本地归档目录", SKIP,
                "归档目录不存在：%s" % archive_root)
        return
    bad = []
    checked = []
    skipped = []
    for name in sorted(os.listdir(archive_root)):
        snap = os.path.join(archive_root, name)
        if not os.path.isdir(snap):
            continue
        try:
            ref = resolve_tag_commit(local, name)
        except RuntimeError:
            skipped.append(name)
            continue
        remote = api_tree(owner, repo, ref, token)
        localfiles, form = archive_snapshot_files(snap, remote)
        only_l, only_r, differ = compare_maps(localfiles, remote)
        checked.append("%s（%s，%d 文件）" % (name, form, len(localfiles)))
        if only_l or only_r or differ:
            bad.append("%s：%s %s" % (name, form, describe(only_l, only_r, differ)))
    note = ""
    if skipped:
        note = ("；跳过 %d 个（本仓库没有同名 tag，属其它项目的归档）：%s"
                % (len(skipped), "、".join(skipped)))
    if bad:
        res.add(6, "远端每个 tag 树 == 本地归档目录", FAIL, str(bad[:5]) + note)
    elif not checked:
        res.add(6, "远端每个 tag 树 == 本地归档目录", SKIP,
                "归档目录里没有一个能对上本仓库的 tag" + note)
    else:
        res.add(6, "远端每个 tag 树 == 本地归档目录", PASS,
                "%d 个归档逐一一致：%s" % (len(checked), "、".join(checked)) + note)


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


def _unfinished(run):
    """这个 run 还在跑吗？

    第 8 项与第 8b 项**必须用同一个判据** —— 否则会出现「第 8 项说还在进行中、
    第 8b 项却拿注解数下了结论」这种自相矛盾的报告。
    已结束却没有结论（`status=completed` + `conclusion=None`）**不算进行中**：
    那是真的异常，应当走失败分支。
    """
    return run.get("conclusion") is None and run.get("status") != "completed"


def check_ci(res, owner, repo, expected_sha, token, workflow=CI_WORKFLOW):
    """第 8 / 8b 项：**只认本次提交**。

    原先取「仓库最近 100 条里最新的一条」。仓库一旦有自动化机器人开 PR
    （本项目一上线就发生了），最新的 run 就属于那个 PR，于是这一项**永久失败** ——
    而一个永远亮着的红叉会训练人忽略它，真出问题时也就看不见了。

    取数上做两层限定：请求时带 `head_sha` 过滤，拿到之后**再按 sha 过滤一遍**
    （不依赖服务端一定认这个参数），然后才按时间取最后一条。
    """
    data = fetch_json("%s/repos/%s/%s/actions/runs?per_page=100&head_sha=%s"
                      % (DEFAULT_API, owner, repo, expected_sha), token)
    items = [r for r in data.get("workflow_runs", []) if r.get("head_sha") == expected_sha]
    if not items:
        res.add(8, "CI 本次提交的 run", SKIP,
                "本次提交（%s）名下读不到任何运行记录 —— 可能这个仓库没有配置 CI，"
                "也可能 push 事件被丢弃（新仓库首次推送的竞态见失败特征速查表）。"
                "**这不是通过。**" % expected_sha[:12])
    else:
        scoped = [r for r in items if _is_workflow(r, workflow)]
        if not scoped:
            res.add(8, "CI 本次提交的 run", FAIL,
                    "本次提交有 %d 条运行记录，但没有 `%s` 工作流的 —— 绿的可能不是它。"
                    % (len(items), workflow))
        else:
            scoped.sort(key=lambda r: r.get("created_at", ""))
            latest = scoped[-1]
            conclusion = latest.get("conclusion")
            detail = "%s #%s head=%s conclusion=%s" % (
                latest.get("name"), latest.get("run_number"),
                latest.get("head_sha", "")[:12], conclusion)
            if conclusion == "success":
                res.add(8, "CI 本次提交的 run", PASS, detail)
            elif _unfinished(latest):
                # 刚推完必然撞上这一条。判 FAIL 会让人以为线上坏了 ——
                # 实测（2026-09-29）：`validate #5 conclusion=None`，而同一轮里
                # 「资产与 tag 一致」「latest 落在最高版本」两项已经 PASS。
                res.add(8, "CI 本次提交的 run", PENDING,
                        detail + "（status=%s）—— 该 run **尚未结束**，这不是失败，"
                        "也不算通过；等它跑完再跑一次回验即可" % latest.get("status"))
            else:
                res.add(8, "CI 本次提交的 run", FAIL, detail)
    # 顺带报告本次提交的注解数（判断弃用警告是否已消除）。
    #
    # ⚠️ **不能用「app 是不是 github-actions」来圈定。** 实测（2026-09-29）
    # 平台给依赖更新机器人建的 check run 名字就叫 `Dependabot`、**app slug 也是
    # `github-actions`**，上面挂着一条「`ubuntu-latest` 将迁移到 Ubuntu 26」的平台通知
    # ⇒ 按 app 过滤照样把它算进来，报出一个假 FAIL。
    #
    # 可靠的口径是**归属**：每个 check run 的 `details_url` 形如
    #   https://github.com/<o>/<r>/actions/runs/<run_id>/job/<job_id>
    # 用「本次提交的 `%s` 工作流」那几个 run 的 id 去比，才算「本次提交自己的 CI」。
    # 这条同样适用于任何往仓库里挂检查的第三方 App —— 与它叫什么名字无关。
    ci_run_ids = {r.get("id") for r in items if _is_workflow(r, workflow)}
    checks = fetch_json("%s/repos/%s/%s/commits/%s/check-runs"
                        % (DEFAULT_API, owner, repo, expected_sha), token)
    cruns = checks.get("check_runs", [])

    def _belongs(c):
        du = c.get("details_url") or ""
        return any(rid and ("/actions/runs/%s/" % rid in du
                            or du.rstrip("/").endswith("/actions/runs/%s" % rid))
                   for rid in ci_run_ids)

    mine = [c for c in cruns if _belongs(c)]
    dropped = [(c.get("name"), (c.get("app") or {}).get("slug"))
               for c in cruns if not _belongs(c)]
    excluded = ""
    if dropped:
        excluded = ("；另有 %d 个 check run 不属于本次提交的 `%s` 工作流，未计入：%s"
                    % (len(dropped), workflow,
                       "、".join("%s[%s]" % (n, s) for n, s in dropped[:6])))
    if not ci_run_ids:
        reason = "本次提交名下没有 `%s` 工作流的 run，无从统计" % workflow
        if dropped:
            reason += "（该提交上另有 %d 个 check run，但它们不属于本工作流）" % len(dropped)
        res.add("8b", "本次提交的注解数（只算 Actions，附带判定）", SKIP, reason)
        return
    unfinished = [r for r in items if _is_workflow(r, workflow) and _unfinished(r)]
    if unfinished:
        # 注解数只有在 run 结束后才是终值 —— run 还在跑就报「注解全 0」是软失败。
        res.add("8b", "本次提交的注解数（只算 Actions，附带判定）", PENDING,
                "有 %d 个 `%s` 的运行尚未结束（%s）⇒ 注解数还不是终值，不能据此判定。"
                "**这不是通过**，也不是失败；等它跑完再跑一次回验。"
                % (len(unfinished), workflow,
                   "、".join("#%s(status=%s)" % (r.get("run_number"), r.get("status"))
                             for r in unfinished[:3])) + excluded)
        return
    counts = [(c.get("name"), (c.get("output") or {}).get("annotations_count")) for c in mine]
    if counts:
        res.add("8b", "本次提交的注解数（只算 Actions，附带判定）",
                PASS if all(n == 0 for _, n in counts) else FAIL,
                "、".join("%s=%s" % (n, c) for n, c in counts) + excluded)
    else:
        res.add("8b", "本次提交的注解数（只算 Actions，附带判定）", SKIP,
                "`%s` 工作流的 run 存在，但没取到它名下的 check run"
                "（可能尚未上报）—— **这不是通过**" % workflow + excluded)


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


# ------------------------------------------------------------ 第 10b 项：平台设置


def _dig(data, *path):
    """按路径从嵌套响应里取值，取不到返回 `None`。

    ⚠️ **`None` 是「读不到」，不是 `False`。** 这两者混为一谈，就会出现
    「没读出来」被写成「没有开启」的假 FAIL —— 或者更糟，被写成「没问题」。
    """
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _yn(value, err=None):
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return ("读不到(%s)" % err) if err else "未返回该字段"


def _try_json(url, token=None):
    """取一个端点，返回 `(data, err)`。**任何**失败都只记原因，不外抛 ——

    平台设置分散在多个端点，其中一个 404（**功能未启用就是 404，不是 `false`**）
    不该让整项变成「该检查抛异常」，也不该把别的端点一起带崩。
    """
    try:
        return fetch_json(url, token), None
    except Exception as exc:                       # 网络 / 权限 / 404 / 限流都走这里
        code = getattr(exc, "code", None)
        reason = str(getattr(exc, "reason", "") or "").strip()
        # ⚠️ **必须把原因带上**：`403` 既可能是「没有权限」，也可能是
        # 「匿名调用的次数用完了」。前者要去换凭据，后者只要等一会儿或带上令牌 ——
        # 只留一个状态码，等于把两种完全不同的处置方式混成一条。
        if code:
            return None, "HTTP %s%s" % (code, (": %s" % reason) if reason else "")
        return None, "%s: %s" % (type(exc).__name__, exc)


# **只有这两条是硬判**，依据是本技能自己的「三道闸」：公开即已泄露、事后撤回治不了本，
# 所以平台侧那两道闸必须是开的（第一道 = 本地发布前隐私扫描，第三道 = 建 tag 前人工清单）。
# 其余几项各自都有真实取舍 —— 例如依赖更新的安全更新机器人会带来 PR 噪音 ——
# 状态照实打印，但**不代替使用者拍板**。一项永远亮着的红/黄，会训练人忽略它。
_HARD_GATES = (("secret_scanning", "密钥扫描"),
               ("secret_scanning_push_protection", "推送保护"))


def check_platform_settings(res, owner, repo, token, branch="main"):
    """第 10b 项：平台安全设置的**只读核验**。

    这一项的存在理由是一条实测更正：早先把「平台侧设置」写成
    「`security_and_analysis` **只对管理员返回** ⇒ 无法核验」，于是维护清单里有三项
    永远标着「无法核验」。**一项永远亮着的「无法核验」和永远亮着的红叉一样，
    会训练人忽略它** —— 而实测（2026-09-29）有管理员凭据时这些端点读得到、也写得动
    （本轮就用 API 开掉了私密漏洞上报）。

    所以本项的口径是：**读得到就据实判，读不到就明确写「无法核验」**，绝不含糊过去。
    """
    base = "%s/repos/%s/%s" % (DEFAULT_API, owner, repo)
    info, err = _try_json(base, token)
    if info is None:
        res.add("10b", CHECK_TITLES["10b"], SKIP,
                "读仓库信息失败（%s）⇒ 无法核验平台设置。**这不是通过。**" % err)
        return
    sec = info.get("security_and_analysis")
    if not isinstance(sec, dict):
        res.add("10b", CHECK_TITLES["10b"], SKIP,
                "响应里没有 `security_and_analysis` —— 该字段**只对管理员返回**，"
                "说明当前凭据没有管理员权限 ⇒ **无法核验**（既不是通过，也不是失败）。"
                "换管理员凭据再跑，或去仓库的 Security 设置里人工核对。")
        return

    states = dict((key, _dig(sec, key, "status")) for key, _ in _HARD_GATES)
    off = [label for key, label in _HARD_GATES if states.get(key) == "disabled"]
    unknown = [label for key, label in _HARD_GATES if states.get(key) is None]

    extra = []

    pvr, e = _try_json(base + "/private-vulnerability-reporting", token)
    extra.append("私密漏洞上报=%s" % _yn(_dig(pvr, "enabled"), e))

    asf, e = _try_json(base + "/automated-security-fixes", token)
    extra.append("依赖更新的安全更新=%s" % _yn(_dig(asf, "enabled"), e))

    imm, e = _try_json(base + "/immutable-releases", token)
    # 这个端点**未启用时返回 404**（不是 `enabled: false`）—— 别把 404 读成「取数失败」。
    if e and "404" in e:
        extra.append("不可变发布=未启用（404）")
    else:
        extra.append("不可变发布=%s" % _yn(_dig(imm, "enabled"), e))

    rs, e = _try_json(base + "/rulesets", token)
    extra.append("规则集=%s" % (("读不到(%s)" % e) if rs is None else "%d 条" % len(rs)))

    prot, e = _try_json(base + "/branches/%s/protection" % branch, token)
    if e and "404" in e:
        extra.append("分支保护=无（404）")
    else:
        extra.append("分支保护=%s" % ("有" if prot is not None else "读不到(%s)" % e))

    fk, e = _try_json(base + "/actions/permissions/fork-pr-contributor-approval", token)
    extra.append("外部贡献者批准=%s" % (_dig(fk, "approval_policy") if fk is not None
                                   else "读不到(%s)" % e))

    body = "、".join(extra)
    if off:
        res.add("10b", CHECK_TITLES["10b"], FAIL,
                "**%s处于关闭状态** —— 这是「三道闸」里的第二道：推上公开仓库就按"
                "**已泄露**处理（永久存档、搜索索引、别人的 fork 都去不掉），"
                "事后撤回治不了本。去仓库的 Security 设置里打开。其余：%s"
                % ("、".join(off), body))
    elif unknown:
        res.add("10b", CHECK_TITLES["10b"], SKIP,
                "%s 的状态**读不到** ⇒ 无法判断，**这不是通过**（读到的原始值：%s）。"
                "其余：%s" % ("、".join(unknown), states, body))
    else:
        res.add("10b", CHECK_TITLES["10b"], PASS,
                "密钥扫描 / 推送保护均已开启；以下几项**只报不判**（各有取舍）：" + body)


# ------------------------------------------------------------------ 主流程


def summarize(res):
    """汇总行与结论、退出码。**抽成函数是为了能单独测** —— 这套计数规则是本工具的
    信誉所在：跳过与「进行中」都必须被看见，而「全部通过」这句话只在真的全通过时说。

    返回 `(汇总行, 结论行列表, 退出码)`。**「进行中」退出码是 0**：它不是失败；
    但结论行绝不会因此说出「全部通过」。
    """
    line = "%d 通过 / %d 失败 / %d 跳过" % (len(res.passes), len(res.fails), len(res.skips))
    if res.pending:
        line += " / %d 进行中" % len(res.pending)
    if res.fails:
        return line, ["回验未通过"], 1
    if res.pending:
        return line, ["回验未发现失败，但有 %d 项仍在进行中（%s）—— 这**不等于**全部通过；"
                      "等它跑完再跑一次即可"
                      % (len(res.pending), "、".join(str(i["no"]) for i in res.pending))], 0
    if res.skips:
        return line, ["回验未发现失败，但有 %d 项被跳过 —— 这**不等于**全部通过"
                      % len(res.skips)], 0
    return line, ["回验全部通过（%d 项）" % len(res.passes)], 0


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
    ap.add_argument("--connect-host", metavar="HOST",
                    help="把所有主机名都解析到 HOST 再连（SNI 与证书校验照旧用真实域名）。"
                         "用于主机名被 hosts 劫持、由本地加速器按 SNI 转发的环境 —— "
                         "那种环境下第 9 项的资产下载会因重定向域名解析失败而做不了（见函数说明）")
    ap.add_argument("--offline", action="store_true", help="不发起任何网络请求")
    args = ap.parse_args(argv)

    if args.connect_host:
        force_connect_host(args.connect_host)

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
            # 下面的每一项都各自隔离：见 _guard 的说明。
            _guard(res, 3, check_history_unchanged, res, refs, args.baseline, args.tag)
            _guard(res, 4, check_remote_main_tree, res, args.owner, args.repo, refs, local, token)
            _guard(res, 5, check_remote_tag_tree, res, args.owner, args.repo, local, args.tag, token)
            _guard(res, 6, check_archive_snapshots, res, args.owner, args.repo, local,
                   args.archive_root, token)
            _guard(res, 7, check_all_tags_local_vs_remote, res, args.owner, args.repo, local,
                   remote_tags, token)
            head = run_git(["rev-parse", "HEAD"], cwd=local).decode().strip()
            _guard(res, 8, check_ci, res, args.owner, args.repo, head, token, args.ci_workflow)
            _guard(res, "9b", check_latest_release, res, args.owner, args.repo, local, token)
            with tempfile.TemporaryDirectory() as tmp:
                _guard(res, 9, check_release_asset, res, args.owner, args.repo, local,
                       args.tag, token, tmp, args.asset_name)
            _guard(res, "10b", check_platform_settings, res, args.owner, args.repo, token)
        except Exception as exc:          # 明确报告，不静默通过
            res.add("!", "远端检查中断", FAIL,
                    "%s: %s（中断之后的项保持「未执行」—— 既不是通过，也不是失败）"
                    % (type(exc).__name__, exc))

    for item in sorted(res.items, key=lambda i: report_order(i["no"])):
        print("[%s] %s %s%s" % (item["status"], item["no"], item["name"],
                                ("  —— " + item["detail"]) if item["detail"] else ""))
    line, notes, code = summarize(res)
    print("\n" + line)
    for note in notes:
        print(note)
    return code


if __name__ == "__main__":
    sys.exit(main())
