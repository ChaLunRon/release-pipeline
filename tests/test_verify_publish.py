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


class TestCheckCi(Base):
    def _fake(self, conclusion, head, annotations=0):
        def fake(url, token=None):
            if "/actions/runs" in url:
                return {"workflow_runs": [
                    {"name": "validate", "run_number": 1, "conclusion": "success",
                     "head_sha": "old", "created_at": "2026-01-01T00:00:00Z"},
                    {"name": "validate", "run_number": 2, "conclusion": conclusion,
                     "head_sha": head, "created_at": "2026-01-02T00:00:00Z"},
                ]}
            return {"check_runs": [{"name": "validate",
                                    "output": {"annotations_count": annotations}}]}
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

    def test_head_sha_mismatch_fails(self):
        vp.fetch_json = self._fake("success", "other")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 1)

    def test_non_success_conclusion_fails(self):
        vp.fetch_json = self._fake("failure", "abc")
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        self.assertEqual(len(res.fails), 1)

    def test_annotation_count_reported_as_failure(self):
        vp.fetch_json = self._fake("success", "abc", annotations=1)
        res = vp.Result()
        vp.check_ci(res, "o", "r", "abc", None)
        bad = [i for i in res.fails if i["no"] == "8b"]
        self.assertEqual(len(bad), 1)
        self.assertIn("validate=1", bad[0]["detail"])


class TestCheckReleaseAsset(Base):
    def _run(self, zip_bytes):
        repo = make_repo(self.tmp, tag="1.0")
        vp.fetch_json = lambda url, token=None: {
            "assets": [{"browser_download_url": "https://downloads.invalid/a.zip"}]}
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
        repo = make_repo(self.tmp, tag="1.0")
        vp.fetch_json = lambda url, token=None: {"assets": []}
        res = vp.Result()
        vp.check_release_asset(res, "o", "r", repo, "1.0", None, self.tmp)
        self.assertEqual(len(res.fails), 1)
        self.assertIn("没有资产", res.fails[0]["detail"])


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
        self.assertNotIn("九项回验全部通过", out)

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


if __name__ == "__main__":
    unittest.main()
