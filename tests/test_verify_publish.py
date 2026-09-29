#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_publish.py 的离线单元测试。

**全部离线**：网络出口（`fetch_json` / `fetch_bytes`）一律用桩替换，
需要仓库的地方都在临时目录里现建。断网或挂代理后结果不变。

夹具里的令牌/地址一律用字符串拼接构造 —— 完整字面量会被平台的密钥扫描
当成真实凭据，也会被本仓库自己的可移植性规则命中。
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify_publish as vp  # noqa: E402

# 网络出口的原始实现 —— 每个用例结束后恢复，避免桩泄漏到下一个用例。
ORIG_FETCH_JSON = vp.fetch_json
ORIG_FETCH_BYTES = vp.fetch_bytes

TOKEN_PREFIX = "ghp" + "_"
FAKE_TOKEN = TOKEN_PREFIX + "A" * 36
EMPTY_BLOB_SHA = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
SAMPLE = {"a.txt": "alpha\n", "docs/b.md": "# 标题\n"}


def write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def git(repo, *args):
    proc = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError("git %s 失败：%s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")))
    return proc.stdout


def make_repo(base, files=None, tag="1.0", annotated=True):
    root = os.path.join(base, "repo")
    os.makedirs(root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "tester@test.invalid")
    git(root, "config", "user.name", "Tester")
    for rel, text in (files or SAMPLE).items():
        write(os.path.join(root, rel), text)
    git(root, "add", "-A")
    git(root, "commit", "-m", "初始提交")
    if tag:
        if annotated:
            git(root, "tag", "-a", tag, "-m", "版本 %s" % tag)
        else:
            git(root, "tag", tag)
    return root


def build_zip(name, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, text in files.items():
            zf.writestr("%s/%s" % (name, rel), text)
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="verify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, vp, "fetch_json", ORIG_FETCH_JSON)
        self.addCleanup(setattr, vp, "fetch_bytes", ORIG_FETCH_BYTES)


class TestBlobSha(Base):
    def test_empty_blob(self):
        self.assertEqual(vp.blob_sha(b""), EMPTY_BLOB_SHA)

    def test_matches_git_hash_object(self):
        repo = make_repo(self.tmp, {"x.txt": "alpha\n"})
        expected = git(repo, "hash-object", "x.txt").decode().strip()
        with open(os.path.join(repo, "x.txt"), "rb") as fh:
            self.assertEqual(vp.blob_sha(fh.read()), expected)


class TestCompareMaps(unittest.TestCase):
    def test_three_columns(self):
        left = {"a": "1", "b": "2", "c": "3"}
        right = {"a": "1", "b": "X", "d": "4"}
        only_l, only_r, differ = vp.compare_maps(left, right)
        self.assertEqual(only_l, ["c"])
        self.assertEqual(only_r, ["d"])
        self.assertEqual(differ, ["b"])

    def test_identical(self):
        same = {"a": "1"}
        self.assertEqual(vp.compare_maps(same, same), ([], [], []))

    def test_describe_counts(self):
        text = vp.describe(["x"], [], ["y", "z"])
        self.assertIn("左独有 1", text)
        self.assertIn("右独有 0", text)
        self.assertIn("内容不同 2", text)


class TestLocalGitHelpers(Base):
    def test_local_tree_keys_and_blobs(self):
        repo = make_repo(self.tmp)
        tree = vp.local_tree(repo, "HEAD")
        self.assertEqual(sorted(tree), ["a.txt", "docs/b.md"])
        for path, sha in tree.items():
            with open(os.path.join(repo, path), "rb") as fh:
                self.assertEqual(sha, vp.blob_sha(fh.read()))

    def test_resolve_annotated_tag_to_commit(self):
        repo = make_repo(self.tmp, tag="1.0", annotated=True)
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        self.assertEqual(vp.resolve_tag_commit(repo, "1.0"), head)

    def test_resolve_lightweight_tag(self):
        repo = make_repo(self.tmp, tag="1.0", annotated=False)
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        self.assertEqual(vp.resolve_tag_commit(repo, "1.0"), head)

    def test_dir_blobs_skips_git_directory(self):
        root = os.path.join(self.tmp, "snap")
        write(os.path.join(root, "a.txt"), "alpha\n")
        write(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/main\n")
        blobs = vp.dir_blobs(root)
        self.assertEqual(sorted(blobs), ["a.txt"])

    def test_dir_blobs_needs_no_git_dir(self):
        root = os.path.join(self.tmp, "plain")
        write(os.path.join(root, "x/y.txt"), "y\n")
        self.assertEqual(sorted(vp.dir_blobs(root)), ["x/y.txt"])


class TestHistoryUnchanged(Base):
    def test_skip_without_baseline(self):
        res = vp.Result()
        vp.check_history_unchanged(res, {}, None)
        self.assertEqual(len(res.skips), 1)
        self.assertEqual(len(res.fails), 0)

    def test_pass_when_unchanged(self):
        path = os.path.join(self.tmp, "baseline.json")
        write(path, '{"refs/tags/0.9": "abc"}')
        res = vp.Result()
        vp.check_history_unchanged(res, {"refs/tags/0.9": "abc"}, path)
        self.assertEqual(len(res.passes), 1)

    def test_fail_when_changed(self):
        path = os.path.join(self.tmp, "baseline.json")
        write(path, '{"refs/tags/0.9": "abc"}')
        res = vp.Result()
        vp.check_history_unchanged(res, {"refs/tags/0.9": "def"}, path)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("历史被动过", res.fails[0]["detail"])

    def test_skip_when_baseline_missing(self):
        res = vp.Result()
        vp.check_history_unchanged(res, {}, os.path.join(self.tmp, "nope.json"))
        self.assertEqual(len(res.skips), 1)

    def test_non_tag_refs_are_not_history(self):
        """回归（2026-09-29 实测抓到的第四个假阳性）。

        基线里还记着 `HEAD` / 分支 / PR 引用。推送本身**必然**移动 `HEAD` 与 `main`；
        关掉一个机器人开的 PR 又会**删掉** `refs/heads/dependabot/*` 与 `refs/pull/*`。
        原先拿全部 ref 比，于是每次都报「历史被动过」—— 实测差异 5 处，
        **没有一处是 tag**。本项要守的是「已发布的 tag 不许被就地改写」，所以只比 tag。
        """
        path = os.path.join(self.tmp, "baseline.json")
        write(path, json.dumps({
            "HEAD": "old-head",
            "refs/heads/main": "old-main",
            "refs/heads/dependabot/x": "gone",
            "refs/pull/1/merge": "gone-too",
            "refs/tags/3.1": "same",
        }))
        now = {"HEAD": "new-head", "refs/heads/main": "new-main",
               "refs/tags/3.1": "same"}
        res = vp.Result()
        vp.check_history_unchanged(res, now, path)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("不计入", res.passes[0]["detail"])

    def test_newly_pushed_tag_is_excluded(self):
        """本次新推的 tag 本来就该是新的 —— 不排除它，本项必然失败。"""
        path = os.path.join(self.tmp, "baseline.json")
        write(path, json.dumps({"refs/tags/3.1": "same", "refs/tags/4.0": "absent-before"}))
        res = vp.Result()
        vp.check_history_unchanged(res, {"refs/tags/3.1": "same", "refs/tags/4.0": "brand-new"},
                                   path, tag="4.0")
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("已排除", res.passes[0]["detail"])

    def test_a_real_tag_change_is_still_caught(self):
        """收窄口径之后，**真正的 tag 被重指**仍然必须被抓到。"""
        path = os.path.join(self.tmp, "baseline.json")
        write(path, json.dumps({"refs/tags/3.1": "abc", "refs/tags/3.1^{}": "abc-peeled"}))
        res = vp.Result()
        vp.check_history_unchanged(res, {"refs/tags/3.1": "abc", "refs/tags/3.1^{}": "MOVED"}, path)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("3.1^{}", res.fails[0]["detail"])

    def test_only_the_new_tag_in_baseline_skips(self):
        """基线里除新 tag 没有别的 tag（仓库第一次发布）⇒ 跳过，不假装通过。"""
        path = os.path.join(self.tmp, "baseline.json")
        write(path, json.dumps({"refs/tags/1.0": "x"}))
        res = vp.Result()
        vp.check_history_unchanged(res, {"refs/tags/1.0": "x"}, path, tag="1.0")
        self.assertEqual(len(res.skips), 1)
        self.assertEqual(len(res.passes), 0)


class TestTagSets(Base):
    def test_symmetric_difference_empty(self):
        repo = make_repo(self.tmp, tag="1.0")
        refs = {"refs/tags/1.0": "x", "refs/heads/main": "y"}
        res = vp.Result()
        vp.check_tag_sets(res, refs, repo)
        self.assertEqual(len(res.passes), 1)

    def test_extra_local_tag_detected(self):
        repo = make_repo(self.tmp, tag="1.0")
        git(repo, "tag", "9.9")
        refs = {"refs/tags/1.0": "x"}
        res = vp.Result()
        vp.check_tag_sets(res, refs, repo)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("9.9", res.fails[0]["detail"])

    def test_missing_remote_tag_detected(self):
        repo = make_repo(self.tmp, tag="1.0")
        refs = {"refs/tags/1.0": "x", "refs/tags/1.1": "z"}
        res = vp.Result()
        vp.check_tag_sets(res, refs, repo)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("1.1", res.fails[0]["detail"])


class TestApiTree(Base):
    def test_filters_non_blob_entries(self):
        vp.fetch_json = lambda url, token=None: {"tree": [
            {"path": "a.txt", "type": "blob", "sha": "1"},
            {"path": "docs", "type": "tree", "sha": "2"},
            {"path": "link", "type": "commit", "sha": "3"},
        ]}
        self.assertEqual(vp.api_tree("o", "r", "sha"), {"a.txt": "1"})


class TestTagSetsPeel(Base):
    """回归：annotated tag 在 `ls-remote` 里**成对出现**（`X` 与 `X` 加一个解引用尾巴），
    本地 `git tag --list` 只列干净名字。

    不剥那个后缀，第 2 项会**每次**都报「有差异」，而且差异数恰好等于 tag 个数 ——
    看起来像线上少了 tag，其实只是取数口径没对齐。
    """

    def test_peel_suffix_is_stripped(self):
        repo = make_repo(self.tmp, tag="1.0")
        refs = {"refs/tags/1.0": "x", "refs/tags/1.0" + "^" + "{}": "y",
                "refs/heads/main": "z"}
        res = vp.Result()
        vp.check_tag_sets(res, refs, repo)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertEqual(len(res.passes), 1)

    def test_peel_suffix_does_not_hide_a_real_extra_tag(self):
        repo = make_repo(self.tmp, tag="1.0")
        refs = {"refs/tags/1.0": "x", "refs/tags/1.0" + "^" + "{}": "y",
                "refs/tags/9.9": "z", "refs/tags/9.9" + "^" + "{}": "w"}
        res = vp.Result()
        vp.check_tag_sets(res, refs, repo)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("9.9", res.fails[0]["detail"])


class TestCheckCi(Base):
    HEAD_RUN = 200
    OLD_RUN = 100

    def _fake(self, conclusion, head, annotations=0, extra_checkruns=(),
              status="completed"):
        """check run 要用 `details_url` 表明它**归属于哪个 run**。

        第 8b 项按归属取数，不再按 app 名字 —— 因为平台给依赖更新机器人建的 check run
        **app slug 也是 `github-actions`**（2026-09-29 实测），按名字过滤会把它算进来。
        """
        head_run = self.HEAD_RUN

        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"id": self.OLD_RUN, "name": "validate", "run_number": 1,
                     "conclusion": "success", "head_sha": "old", "status": "completed",
                     "created_at": "2026-01-01T00:00:00Z"},
                    {"id": head_run, "name": "validate", "run_number": 2,
                     "conclusion": conclusion, "head_sha": head, "status": status,
                     "created_at": "2026-01-02T00:00:00Z"},
                ]}
            runs = [{"name": "validate", "app": {"slug": "github-actions"},
                     "details_url": "https://github.com/o/r/actions/runs/%d/job/7" % head_run,
                     "output": {"annotations_count": annotations}}]
            runs.extend(extra_checkruns)
            return {"check_runs": runs}
        return fake

    def test_latest_run_pass(self):
        vp.fetch_json = self._fake("success", "abc")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0)
        self.assertIn("8b", [i["no"] for i in res.passes])

    def test_latest_run_picks_newest_not_first(self):
        """列表里的第一条是旧运行 —— 必须按时间取最后一条。"""
        vp.fetch_json = self._fake("success", "abc")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        detail = [i for i in res.items if i["no"] == 8][0]["detail"]
        self.assertIn("#2", detail)

    def test_run_of_another_commit_is_ignored(self):
        """回归：原先取「仓库最近 100 条里最新的一条」。

        仓库一旦有机器人开 PR（本项目一上线就发生了），最新的 run 就属于那个 PR，
        这一项于是**永久失败** —— 而永远亮着的红叉会训练人忽略红叉。
        这里把「属于本次提交的旧 run」与「不属于本次提交的新 run」都放进列表，
        正确答案是认前者。
        """
        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"id": 1, "name": "validate", "run_number": 7, "conclusion": "success",
                     "head_sha": "abc", "created_at": "2026-01-01T00:00:00Z"},
                    {"id": 2, "name": "validate", "run_number": 8, "conclusion": "failure",
                     "head_sha": "someone-elses-sha",
                     "created_at": "2026-01-09T00:00:00Z"},
                ]}
            return {"check_runs": [{"name": "validate", "app": {"slug": "github-actions"},
                                    "details_url": "https://github.com/o/r/actions/runs/1/job/7",
                                    "output": {"annotations_count": 0}}]}
        vp.fetch_json = fake
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("#7", [i for i in res.items if i["no"] == 8][0]["detail"])

    def test_no_run_for_this_commit_is_not_a_pass(self):
        """本次提交名下没有任何运行记录 ⇒ 跳过（且**绝不能**写成通过）。"""
        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"id": 8, "name": "validate", "run_number": 8, "conclusion": "success",
                     "head_sha": "someone-elses-sha",
                     "created_at": "2026-01-09T00:00:00Z"},
                ]}
            return {"check_runs": []}
        vp.fetch_json = fake
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0)
        self.assertEqual([i["no"] for i in res.skips], [8, "8b"])

    def test_runs_without_the_validate_workflow_fail(self):
        """本次提交有运行记录，但没有校验工作流 ⇒ FAIL（绿的可能不是它）。"""
        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"id": 1, "name": "release", "run_number": 1, "conclusion": "success",
                     "head_sha": "abc", "created_at": "2026-01-02T00:00:00Z"},
                ]}
            return {"check_runs": []}
        vp.fetch_json = fake
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("validate", res.fails[0]["detail"])

    def test_non_success_conclusion_fails(self):
        vp.fetch_json = self._fake("failure", "abc")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 1)

    def test_unfinished_run_is_pending_not_failed(self):
        """回归（2026-09-29 真实发布上的实况）：刚推完就回验，`conclusion` 是 `None`。

        那不是失败 —— 同一次回验里「资产与 tag 逐文件一致」与「latest 落在最高版本」
        两项已经 PASS，说明只是流水线还没跑完。判 FAIL 会让人以为线上坏了。
        """
        vp.fetch_json = self._fake(None, "abc", status="in_progress")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertEqual([(i["no"], i["status"]) for i in res.items],
                         [(8, vp.PENDING), ("8b", vp.PENDING)])
        self.assertIn("尚未结束", [i for i in res.items if i["no"] == 8][0]["detail"])

    def test_unfinished_run_does_not_turn_zero_annotations_into_a_pass(self):
        """run 没结束时报「注解全 0」是典型的软失败 —— 那时注解数还不是终值。"""
        vp.fetch_json = self._fake(None, "abc", annotations=0, status="queued")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.passes), 0, res.items)
        self.assertIn("还不是终值", [i for i in res.items if i["no"] == "8b"][0]["detail"])

    def test_completed_run_without_conclusion_is_still_a_failure(self):
        """**收窄口径 ≠ 放宽**：已经结束却没有结论，仍然是失败。"""
        vp.fetch_json = self._fake(None, "abc", status="completed")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual([i["no"] for i in res.fails], [8])
        self.assertEqual(len(res.pending), 0)

    def test_annotation_count_reported_as_failure(self):
        vp.fetch_json = self._fake("success", "abc", annotations=1)
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        bad = [i for i in res.fails if i["no"] == "8b"]
        self.assertEqual(len(bad), 1)
        self.assertIn("validate=1", bad[0]["detail"])

    def test_checkrun_from_another_app_is_excluded(self):
        """回归：原先不区分来源地统计注解，把依赖更新机器人的检查算到了本次头上。"""
        extra = [{"name": "Dependabot", "app": {"slug": "dependabot", "name": "Dependabot"},
                  "details_url": "https://dependabot-api.githubapp.com",
                  "output": {"annotations_count": 3}}]
        vp.fetch_json = self._fake("success", "abc", annotations=0, extra_checkruns=extra)
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0, res.items)
        detail = [i for i in res.items if i["no"] == "8b"][0]["detail"]
        self.assertIn("validate=0", detail)
        self.assertIn("未计入", detail)

    def test_platform_checkrun_under_the_actions_app_is_excluded(self):
        """回归（2026-09-29 实测抓到的第五个假阳性）。

        平台给依赖更新机器人建的 check run **app slug 就是 `github-actions`**，
        名字叫 `Dependabot`，还挂着一条平台通知。按 app 过滤拦不住它 ——
        必须按**归属**（`details_url` 里的 run id）判，它属于**另一个工作流的 run**。
        """
        extra = [{"name": "Dependabot", "app": {"slug": "github-actions"},
                  "details_url": "https://github.com/o/r/actions/runs/999/job/1",
                  "output": {"annotations_count": 1}}]
        vp.fetch_json = self._fake("success", "abc", annotations=0, extra_checkruns=extra)
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 0, res.items)
        detail = [i for i in res.items if i["no"] == "8b"][0]["detail"]
        self.assertIn("validate=0", detail)
        self.assertIn("Dependabot[github-actions]", detail)

    def test_no_checkrun_of_the_ci_workflow_is_not_a_pass(self):
        """run 在、但它名下的 check run 取不到 ⇒ SKIP，且措辞不得像通过。"""
        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"id": 200, "name": "validate", "run_number": 2,
                     "conclusion": "success", "head_sha": "abc",
                     "created_at": "2026-01-02T00:00:00Z"},
                ]}
            return {"check_runs": []}
        vp.fetch_json = fake
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        item = [i for i in res.items if i["no"] == "8b"][0]
        self.assertEqual(item["status"], vp.SKIP)
        self.assertIn("这不是通过", item["detail"])


class TestCheckReleaseAsset(Base):
    def _run(self, zip_bytes, assets=None, extra=None):
        repo = make_repo(self.tmp, tag="1.0")
        rel = {"assets": assets if assets is not None else [
            {"name": "repo.zip", "browser_download_url": "https://downloads.invalid/a.zip"}]}
        rel.update(extra or {})
        vp.fetch_json = lambda url, token=None: rel
        vp.fetch_bytes = lambda url, token=None: zip_bytes
        res = vp.Result()
        vp.check_release_asset(res, "o", "r", repo, "1.0", None, self.tmp)
        return res

    def test_matching_asset_passes(self):
        res = self._run(build_zip("demo", SAMPLE))
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("2 条目", res.passes[0]["detail"])

    def test_content_mismatch_fails(self):
        res = self._run(build_zip("demo", {"a.txt": "tampered\n", "docs/b.md": "# 标题\n"}))
        self.assertEqual(len(res.fails), 1)
        self.assertIn("内容不同 1", res.fails[0]["detail"])

    def test_multiple_top_dirs_fails(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("one/a.txt", "x")
            zf.writestr("two/b.txt", "y")
        res = self._run(buf.getvalue())
        self.assertEqual(len(res.fails), 1)
        self.assertIn("顶层目录不唯一", res.fails[0]["detail"])

    def test_no_assets_fails(self):
        res = self._run(b"", assets=[])
        self.assertEqual(len(res.fails), 1)
        self.assertIn("没有资产", res.fails[0]["detail"])

    def test_asset_chosen_by_name_not_by_position(self):
        """回归：原先取 `assets[0]`。一个 Release 常同时挂 zip + 校验和 + 签名 + SBOM，
        取第一个等于在赌列表顺序。"""
        good = build_zip("demo", SAMPLE)
        assets = [
            {"name": "checksums.txt", "browser_download_url": "https://downloads.invalid/c.txt"},
            {"name": "repo.zip", "browser_download_url": "https://downloads.invalid/a.zip"},
        ]
        repo = make_repo(self.tmp, tag="1.0")
        vp.fetch_json = lambda url, token=None: {"assets": assets}
        vp.fetch_bytes = lambda url, token=None: (
            b"not a zip" if url.endswith("c.txt") else good)
        res = vp.Result()
        vp.check_release_asset(res, "o", "r", repo, "1.0", None, self.tmp)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("repo.zip", res.passes[0]["detail"])

    def test_expected_asset_missing_fails(self):
        assets = [{"name": "something-else.zip",
                   "browser_download_url": "https://downloads.invalid/x.zip"}]
        res = self._run(b"", assets=assets)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("没有名为", res.fails[0]["detail"])

    def test_draft_release_fails(self):
        res = self._run(build_zip("demo", SAMPLE), extra={"draft": True})
        self.assertEqual(len(res.fails), 1)
        self.assertIn("draft", res.fails[0]["detail"])

    def test_prerelease_fails(self):
        res = self._run(build_zip("demo", SAMPLE), extra={"prerelease": True})
        self.assertEqual(len(res.fails), 1)
        self.assertIn("prerelease", res.fails[0]["detail"])


class TestLatestRelease(Base):
    def _run(self, latest_tag, tags=("1.0", "2.0")):
        repo = make_repo(self.tmp, tag=tags[0])
        for extra_tag in tags[1:]:
            git(repo, "tag", extra_tag)
        vp.fetch_json = lambda url, token=None: {"tag_name": latest_tag}
        res = vp.Result()
        vp.check_latest_release(res, "o", "r", repo, None)
        return res

    def test_latest_on_highest_version_passes(self):
        res = self._run("2.0")
        self.assertEqual(len(res.fails), 0, res.items)

    def test_latest_on_older_version_fails(self):
        res = self._run("1.0")
        self.assertEqual(len(res.fails), 1)
        self.assertIn("本地最高版本是 2.0", res.fails[0]["detail"])

    def test_no_version_tag_skips(self):
        repo = make_repo(self.tmp, tag=None)
        res = vp.Result()
        vp.check_latest_release(res, "o", "r", repo, None)
        self.assertEqual(len(res.passes), 0)
        self.assertEqual(len(res.skips), 1)


class TestCredentialResidue(Base):
    def test_clean_repo_passes(self):
        repo = make_repo(self.tmp)
        res = vp.Result()
        vp.check_credentials(res, repo)
        self.assertEqual(len(res.passes), 1)

    def test_token_in_config_detected(self):
        repo = make_repo(self.tmp)
        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write('\n[remote "origin"]\n\turl = https://%s@github.invalid/o/r.git\n' % FAKE_TOKEN)
        res = vp.Result()
        vp.check_credentials(res, repo)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("令牌字面量", res.fails[0]["detail"])

    def test_token_beyond_former_two_mib_window_is_detected(self):
        """回归：原先只读每个文件的前 2 MiB 却报 PASS —— 典型的软失败
        （截断扫描的结果被当成了完整结论）。对象库里的 packfile 很容易超过 2 MiB，
        所以这条必须有回归用例守住。"""
        repo = make_repo(self.tmp)
        write(os.path.join(repo, ".git", "objects", "pack-fake.pack"),
              "x" * (3 * 1024 * 1024) + "\n" + FAKE_TOKEN + "\n")
        res = vp.Result()
        vp.check_credentials(res, repo)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("令牌字面量", res.fails[0]["detail"])

    def test_pass_message_only_claims_what_was_read(self):
        """结论必须说清是「逐文件读全」的还是「只读了一部分」的。"""
        repo = make_repo(self.tmp)
        res = vp.Result()
        vp.check_credentials(res, repo)
        self.assertIn("逐文件读全", res.passes[0]["detail"])


class _Http404(Exception):
    """`urllib` 的 `HTTPError` 的形状：带 `.code` 与 `.reason`。只借形状，不碰真网络。"""
    code = 404

    def __init__(self, reason=""):
        super(_Http404, self).__init__(reason)
        self.reason = reason


class _Http403(Exception):
    code = 403

    def __init__(self, reason=""):
        super(_Http403, self).__init__(reason)
        self.reason = reason


class TestPlatformSettings(Base):
    """第 10b 项：平台设置的**只读核验**。

    存在理由是一条实测更正：曾把「平台侧设置」写成
    「`security_and_analysis` 只对管理员返回 ⇒ 无法核验」，于是维护清单里有三项
    永远标着「无法核验」。**一项永远亮着的「无法核验」和永远亮着的红叉一样，
    会训练人忽略它** —— 而实测（2026-09-29）有管理员凭据时这些端点读得到、也写得动。
    """

    BASE = "https://api.github.com/repos/o/r"
    ENABLED = {"secret_scanning": {"status": "enabled"},
               "secret_scanning_push_protection": {"status": "enabled"}}

    def _fake(self, sec, pvr=True, immutable=None, rulesets=None, protection=None,
              fork="first_time_contributors", broken=()):
        def fake(url, token=None):
            if url in broken:
                raise _Http404("Not Found")
            if url == self.BASE:
                return {"security_and_analysis": sec} if sec is not None else {}
            if url.endswith("/private-vulnerability-reporting"):
                return {"enabled": pvr}
            if url.endswith("/automated-security-fixes"):
                return {"enabled": False, "paused": False}
            if url.endswith("/immutable-releases"):
                # **未启用就是 404，不是 `enabled: false`。**
                if immutable is None:
                    raise _Http404("Not Found")
                return {"enabled": immutable, "enforced_by_owner": False}
            if url.endswith("/rulesets"):
                return rulesets if rulesets is not None else []
            if "/branches/" in url:
                if protection is None:
                    raise _Http404("Not Found")
                return {"required_status_checks": {}}
            if url.endswith("/fork-pr-contributor-approval"):
                return {"approval_policy": fork}
            raise AssertionError("未预期的端点：%s" % url)
        return fake

    def _check(self, **kw):
        vp.fetch_json = self._fake(**kw)
        res = vp.Result()
        vp.check_platform_settings(res, "o", "r", "token-stub")
        self.assertEqual(len(res.items), 1, res.items)
        return res.items[0], res

    def test_both_gates_open_pass_and_the_rest_are_reported_only(self):
        item, _ = self._check(sec=self.ENABLED)
        self.assertEqual(item["status"], vp.PASS)
        self.assertIn("只报不判", item["detail"])
        # 404 = 功能未启用，**不是**「取数失败」—— 两者读法完全不同
        self.assertIn("不可变发布=未启用（404）", item["detail"])
        self.assertIn("分支保护=无（404）", item["detail"])
        self.assertIn("外部贡献者批准=first_time_contributors", item["detail"])

    def test_push_protection_off_fails_and_says_why(self):
        sec = dict(self.ENABLED, secret_scanning_push_protection={"status": "disabled"})
        item, res = self._check(sec=sec)
        self.assertEqual(item["status"], vp.FAIL)
        self.assertIn("推送保护", item["detail"])
        self.assertIn("已泄露", item["detail"])
        self.assertEqual(len(res.passes), 0)

    def test_missing_security_field_is_skipped_not_passed(self):
        """字段缺失 = 没有管理员权限 ⇒ **无法核验**，绝不能写成「没问题」。"""
        item, res = self._check(sec=None)
        self.assertEqual(item["status"], vp.SKIP)
        self.assertIn("无法核验", item["detail"])
        self.assertIn("管理员", item["detail"])
        self.assertEqual(len(res.passes), 0)

    def test_unknown_gate_status_is_skipped(self):
        """只读得到一个闸的状态 ⇒ 仍然是「无法判断」，不许降级成通过。"""
        item, _ = self._check(sec={"secret_scanning": {"status": "enabled"}})
        self.assertEqual(item["status"], vp.SKIP)
        self.assertIn("推送保护", item["detail"])

    def test_unreadable_repo_is_skipped(self):
        item, _ = self._check(sec=self.ENABLED, broken=(self.BASE,))
        self.assertEqual(item["status"], vp.SKIP)
        self.assertIn("这不是通过", item["detail"])

    def test_one_dead_endpoint_does_not_sink_the_item(self):
        """某个端点挂了，只把那一格写成「读不到」，其余照报 —— 硬判不受影响。"""
        item, _ = self._check(sec=self.ENABLED, broken=(self.BASE + "/rulesets",))
        self.assertEqual(item["status"], vp.PASS)
        self.assertIn("规则集=读不到", item["detail"])

    def test_reason_is_kept_so_a_rate_limit_is_not_read_as_no_permission(self):
        """`403` 既可能是限流、也可能是没权限 —— 只留状态码等于把两种处置方式混成一条。

        这条是实测撞出来的：匿名跑回验时被平台限流，报告只写「HTTP 403」，
        读起来像权限问题，会把人引去换凭据（而其实等一会儿就行）。
        """
        def fake(url, token=None):
            raise _Http403("rate limit exceeded")

        vp.fetch_json = fake
        res = vp.Result()
        vp.check_platform_settings(res, "o", "r", None)
        self.assertEqual(res.items[0]["status"], vp.SKIP)
        self.assertIn("rate limit exceeded", res.items[0]["detail"])


class TestInterruptionSemantics(Base):
    """回归：原先「执行成功才登记」，于是第 4 项抛异常时第 5–9 项**从报告里消失** ——
    报告既不显示它们失败，也不显示它们被跳过。这直接违反了本技能自己的纪律
    「跳过必须显式打印，并在汇总里单独计数」。"""

    # **号码表从 `CHECK_PLAN` 现推**，不要在这里抄一份。
    # 抄一份的话，每加一个检查项都要回来改这个用例 —— 而漏改的表现是
    # 「用例还在、却已经查不到新项」，正好放过它本该守住的那件事。
    # 第 10 项（凭据复查）在本机就能跑，不属于「远端项」。
    PLAN_NUMBERS = tuple(no for no, _ in vp.CHECK_PLAN if no != 10)
    ALL_NUMBERS = tuple(no for no, _ in vp.CHECK_PLAN)

    def test_exception_leaves_every_item_accounted_for(self):
        repo = make_repo(self.tmp, tag="1.0")

        def boom(url):
            raise RuntimeError("模拟限流 / 403")

        self.addCleanup(setattr, vp, "remote_refs", vp.remote_refs)
        vp.remote_refs = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = vp.main(["--owner", "o", "--repo", "r",
                            "--local", repo, "--tag", "1.0"])
        out = buf.getvalue()
        self.assertEqual(code, 1, out)
        # 远端项必须全部以「跳过」现身，而不是从报告里消失
        for no in self.PLAN_NUMBERS:
            self.assertIn("[SKIP] %s " % no, out,
                          "第 %s 项从报告里消失了：\n%s" % (no, out))
        # 凭据复查在本机就能跑，它应当照常出结论（通过或跳过都行，但不能消失）
        self.assertRegex(out, r"\[(PASS|FAIL|SKIP)\] 10 ")
        self.assertIn("远端检查中断", out)
        # 汇总必须体现「有 N 项没跑」，而不是让人从「1 通过」里猜
        self.assertIn("%d 跳过" % len(self.PLAN_NUMBERS), out)

    def test_plan_covers_every_numbered_item_once(self):
        numbers = [no for no, _ in vp.CHECK_PLAN]
        self.assertEqual(sorted(map(str, numbers)),
                         sorted(map(str, self.ALL_NUMBERS)))
        self.assertEqual(len(numbers), len(set(map(str, numbers))),
                         "回验计划里有重复号位 —— 同号位会被覆盖，等于有一项查不到")


class TestPerItemIsolation(Base):
    """回归（2026-09-29 实测）：某一项自己抛异常，不许把**后面的项**一起带崩。

    那一次的实况：第 6 项因「归档目录名不是仓库的 tag」抛 `RuntimeError`，
    第 7–9b 项连跑都没跑（报告里全是「未执行」）—— 而第 9 项恰恰是
    「Release 资产与 tag 是否一致」，**真问题最可能就藏在这些被带崩的项里**。
    """

    def test_guard_turns_exception_into_that_items_failure_only(self):
        res = vp.Result()
        vp._guard(res, 3, lambda *a: (_ for _ in ()).throw(RuntimeError("模拟异常")))
        self.assertEqual([i["no"] for i in res.fails], [3])
        self.assertIn("回验工具", res.fails[0]["detail"])
        # 关键：它只登记了**自己那一项**，没有波及别的项
        self.assertEqual(len(res.items), 1)

    def test_guard_marks_the_item_so_it_never_looks_like_a_pass(self):
        res = vp.Result()
        vp._guard(res, 9, lambda *a: 1 / 0)
        self.assertEqual(res.fails[0]["status"], vp.FAIL)
        self.assertEqual(len(res.passes), 0)

    def test_guard_keeps_the_plan_title(self):
        res = vp.Result()
        vp._guard(res, 6, lambda *a: (_ for _ in ()).throw(ValueError("x")))
        self.assertEqual(res.fails[0]["name"], dict(vp.CHECK_PLAN)[6])


class TestArchiveSnapshots(Base):
    """第 6 项：归档形态有三种，且**归档目录名未必是本仓库的 tag**。"""

    def _snapshot(self, name, files):
        d = os.path.join(self.tmp, "history", name)
        for rel, text in files.items():
            write(os.path.join(d, rel), text)
        return d

    def test_directory_without_matching_tag_is_skipped_not_failed(self):
        """回归（2026-09-29 实测）：`history/1.0/` 是**前身项目**的快照，
        `1.0` 这个 tag 在本仓库里根本没有 ⇒ 原先直接抛异常、带崩整段。"""
        repo = make_repo(self.tmp, tag="2.0")
        self._snapshot("1.0", {"SKILL.md": "前身项目"})
        self._snapshot("2.0", SAMPLE)
        calls = []

        def fake_tree(owner, repo_, sha, token=None):
            calls.append(sha)
            return {rel: vp.blob_sha(text.encode()) for rel, text in SAMPLE.items()}

        self.addCleanup(setattr, vp, "api_tree", vp.api_tree)
        self.addCleanup(setattr, vp, "resolve_tag_commit", vp.resolve_tag_commit)
        vp.api_tree = fake_tree
        res = vp.Result()
        vp.check_archive_snapshots(res, "o", "r", repo, os.path.join(self.tmp, "history"), None)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertEqual(len(res.passes), 1, res.items)
        detail = res.passes[0]["detail"]
        self.assertIn("1.0", detail)          # 跳过了它，并且写明
        self.assertIn("跳过", detail)
        self.assertEqual(len(calls), 1, "不该为没有 tag 的归档去取远端树")

    def test_mismatch_is_still_a_failure(self):
        """跳过逻辑不能把**真不一致**也放过去。"""
        repo = make_repo(self.tmp, tag="2.0")
        self._snapshot("2.0", {"SKILL.md": "内容被改过"})
        self.addCleanup(setattr, vp, "api_tree", vp.api_tree)
        vp.api_tree = lambda o, r, s, t=None: {rel: vp.blob_sha(x.encode())
                                               for rel, x in SAMPLE.items()}
        res = vp.Result()
        vp.check_archive_snapshots(res, "o", "r", repo, os.path.join(self.tmp, "history"), None)
        self.assertEqual(len(res.fails), 1, res.items)

    def test_zip_form_of_archive_is_recognised(self):
        """归档常是「只放一个 zip」的形态，也要能比。"""
        repo = make_repo(self.tmp, tag="2.0")
        d = os.path.join(self.tmp, "history", "2.0")
        os.makedirs(d)
        with open(os.path.join(d, "pkg-2.0.zip"), "wb") as fh:
            fh.write(build_zip("pkg", SAMPLE))
        self.addCleanup(setattr, vp, "api_tree", vp.api_tree)
        vp.api_tree = lambda o, r, s, t=None: {rel: vp.blob_sha(x.encode())
                                               for rel, x in SAMPLE.items()}
        res = vp.Result()
        vp.check_archive_snapshots(res, "o", "r", repo, os.path.join(self.tmp, "history"), None)
        self.assertEqual(len(res.passes), 1, res.items)
        self.assertIn("打包产物", res.passes[0]["detail"])

    def test_archive_root_without_any_matching_tag_skips(self):
        repo = make_repo(self.tmp, tag="9.9")
        self._snapshot("1.0", {"a": "1"})
        res = vp.Result()
        vp.check_archive_snapshots(res, "o", "r", repo, os.path.join(self.tmp, "history"), None)
        self.assertEqual(len(res.skips), 1, res.items)
        self.assertEqual(len(res.passes), 0)

    def test_zip_wins_over_delivery_docs_with_a_colliding_name(self):
        """回归（2026-09-29 实测）。

        归档目录里**同时**放着交付文档与打包产物，而交付文档里的 `README.md` 与
        仓库里的 `README.md` **恰好同名** ⇒ 只看「路径有没有交集」会选中交付文档，
        报出「左独有 4 / 右独有 23」这种看着很严重的差异。
        必须按 (内容完全相同的文件数, 路径重合数) 打分 —— 打包产物 24/24 完胜 0/1。
        """
        repo = make_repo(self.tmp, tag="2.0")
        d = os.path.join(self.tmp, "history", "2.0")
        write(os.path.join(d, "README.md"), "这是交付夹的导航，不是仓库里的那个\n")
        write(os.path.join(d, "CHANGES-2.0.md"), "变更说明\n")
        with open(os.path.join(d, "pkg-2.0.zip"), "wb") as fh:
            fh.write(build_zip("pkg", SAMPLE))
        remote = {rel: vp.blob_sha(x.encode()) for rel, x in SAMPLE.items()}
        self.addCleanup(setattr, vp, "api_tree", vp.api_tree)
        vp.api_tree = lambda o, r, s, t=None: remote
        res = vp.Result()
        vp.check_archive_snapshots(res, "o", "r", repo, os.path.join(self.tmp, "history"), None)
        self.assertEqual(len(res.fails), 0, res.items)
        self.assertIn("打包产物", res.passes[0]["detail"])


class TestConnectHost(Base):
    """`--connect-host`：把解析钉到本机，SNI 与证书校验仍用真实域名。

    实况（2026-09-29）：加速器把 github 域名指到本机、按 SNI 转发，
    `api.github.com` 通，但 Release 资产会 302 到**不在 hosts 里**的域名，
    那个域名解析不出来 ⇒ 第 9 项直接做不了。
    """

    def test_patching_only_redirects_resolution(self):
        import socket
        original = socket.getaddrinfo
        try:
            vp.force_connect_host("127.0.0.1")
            self.assertEqual(vp._CONNECT_HOST, "127.0.0.1")
            infos = socket.getaddrinfo("objects.githubusercontent.com", 443)
            self.assertTrue(all(i[4][0] == "127.0.0.1" for i in infos), infos)
        finally:
            socket.getaddrinfo = original
            vp._CONNECT_HOST = None

    def test_flag_is_off_by_default(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            vp.main(["--help"])
        self.assertIn("--connect-host", buf.getvalue())

    def test_help_says_it_keeps_sni_and_cert_checks(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            vp.main(["--help"])
        self.assertIn("SNI", buf.getvalue())


class TestOfflineCli(Base):
    def test_offline_reports_skips_and_never_claims_full_pass(self):
        repo = make_repo(self.tmp, tag="1.0")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = vp.main(["--owner", "o", "--repo", "r", "--local", repo,
                            "--tag", "1.0", "--offline"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn("跳过", out)
        # ⚠️ 原先这里写的是 `九项回验全部通过` —— 而脚本从来不打这句，
        # 于是这条断言**永远不会失败**（一条测不到东西的用例比没有更糟）。
        # 断言必须打在脚本真会输出的那句话上。
        self.assertNotIn("回验全部通过（", out)
        self.assertIn("%d 跳过" % (len(vp.CHECK_PLAN) - 1), out)

    def test_requires_tag(self):
        repo = make_repo(self.tmp, tag="1.0")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = vp.main(["--owner", "o", "--repo", "r", "--local", repo])
        self.assertEqual(code, 2)

    def test_rejects_non_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = vp.main(["--owner", "o", "--repo", "r", "--local", plain, "--tag", "1.0"])
        self.assertEqual(code, 2)

    def test_credential_check_runs_even_offline(self):
        repo = make_repo(self.tmp, tag="1.0")
        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write('\n[credential]\n\thelper = store --file=/tmp/x\n# %s\n' % FAKE_TOKEN)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = vp.main(["--owner", "o", "--repo", "r", "--local", repo,
                            "--tag", "1.0", "--offline"])
        self.assertEqual(code, 1, buf.getvalue())
        self.assertIn("凭据残留复查", buf.getvalue())


class TestResultBookkeeping(unittest.TestCase):
    def test_counts(self):
        res = vp.Result()
        res.add(1, "a", vp.PASS)
        res.add(2, "b", vp.FAIL, "d")
        res.add(3, "c", vp.SKIP)
        self.assertEqual((len(res.passes), len(res.fails), len(res.skips)), (1, 1, 1))

    def test_pending_is_counted_separately_from_both(self):
        res = vp.Result()
        res.add(1, "a", vp.PASS)
        res.add(2, "b", vp.PENDING, "跑着呢")
        self.assertEqual((len(res.passes), len(res.fails), len(res.pending)), (1, 0, 1))
        line, notes, code = vp.summarize(res)
        self.assertEqual(code, 0, "「进行中」不是失败，不该让整轮回验返回非零")
        self.assertIn("1 进行中", line)
        # 标记取带括号的整句 —— 结论行里「这**不等于**全部通过」这种否定句
        # 也含「全部通过」四个字，拿它当标记会得到一个永远为真的断言。
        self.assertNotIn("回验全部通过（", " ".join(notes))
        self.assertIn("进行中", " ".join(notes))

    def test_full_pass_is_claimed_only_when_there_is_nothing_else(self):
        res = vp.Result()
        res.add(1, "a", vp.PASS)
        _, notes, code = vp.summarize(res)
        self.assertEqual(code, 0)
        self.assertIn("回验全部通过（", notes[0])

    def test_skips_suppress_the_full_pass_wording(self):
        res = vp.Result()
        res.add(1, "a", vp.PASS)
        res.add(2, "b", vp.SKIP)
        _, notes, code = vp.summarize(res)
        self.assertEqual(code, 0)
        self.assertNotIn("回验全部通过（", " ".join(notes))

    def test_failure_wins_over_everything(self):
        res = vp.Result()
        res.add(1, "a", vp.PASS)
        res.add(2, "b", vp.PENDING)
        res.add(3, "c", vp.FAIL)
        _, notes, code = vp.summarize(res)
        self.assertEqual(code, 1)
        self.assertEqual(notes, ["回验未通过"])

    def test_report_order_is_numeric_not_lexicographic(self):
        """「第 10 项」必须排在「第 2 项」之后 —— 否则报告看起来像有重复或漏项，
        而这恰恰是这份报告最不该制造的效果。"""
        order = sorted([1, 10, "10b", 2, 9, "9b", "8b", 8, "!"], key=vp.report_order)
        self.assertEqual(order, ["!", 1, 2, 8, "8b", 9, "9b", 10, "10b"])


class TestCheckerSelfConsistency(unittest.TestCase):
    """守元规则：检查某种字面量的代码，自身不得含有该字面量的完整形态。

    这条不是洁癖 —— 实测踩过：`check_credentials` 里的令牌匹配模式原先写成完整字面量，
    于是**查泄露的代码自己命中了泄露扫描**，而且将来一次全局替换会把它改坏而它静默通过。
    """

    @staticmethod
    def _source():
        with open(vp.__file__, encoding="utf-8") as fh:
            return fh.read()

    def test_source_has_no_token_shaped_literal(self):
        # 守门用的模式同样拼接构造 —— 否则这条用例自己就成了它要抓的违规样本。
        shaped = re.compile("|".join(re.escape(p) + r"[A-Za-z0-9_]{20,}"
                                    for p in ("ghp" + "_", "github" + "_pat" + "_")))
        self.assertIsNone(
            shaped.search(self._source()),
            "verify_publish.py 自身含令牌形态的字面量：密钥扫描器会误报，"
            "且一次全局替换会把这条规则改坏。请用字符串拼接构造前缀。")

    def test_token_prefixes_are_built_by_concatenation(self):
        self.assertIn('"ghp" + "_"', self._source())

    def test_peel_suffix_is_built_by_concatenation(self):
        """解引用后缀同样拼接构造 —— 同一元规则，别「顺手简化」回去。"""
        self.assertIn('"^" + "{}"', self._source())


if __name__ == "__main__":
    unittest.main()
