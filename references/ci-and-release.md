# CI 与自动发布

## 目录

- [一、为什么分成两个工作流](#一为什么分成两个工作流)
- [二、校验工作流模板](#二校验工作流模板)
- [三、发布工作流模板](#三发布工作流模板)
- [四、发布物必须可由 tag 重现](#四发布物必须可由-tag-重现)
- [五、`actions/*` 的大版本纪律](#五actions-的大版本纪律)
- [六、验收 CI 修复：重放 + 对照](#六验收-ci-修复重放--对照)
- [七、判断 CI 现状只看最新一次运行](#七判断-ci-现状只看最新一次运行)

## 一、为什么分成两个工作流

- **结构校验只需要读权限**；**建 Release 需要写权限**。
- 拆成两个文件，就能把写权限限制在真正需要的那一条路径上，不放大例行检查的权限。
- 还有一个现实好处：校验失败时，输出里只有校验结论，不会掺进发布步骤的噪音。

约定：

| 工作流 | 触发 | 权限 |
|---|---|---|
| `validate.yml` | 推主分支 / PR / 手动 | `contents: read` |
| `release.yml` | 推 tag / 手动（带回填开关） | `contents: write` |

## 二、校验工作流模板

```yaml
name: validate

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: validate-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    name: 规范自检 + 单元测试 (py${{ matrix.python-version }})
    # 不要用 `-latest`：runner 镜像会升级，而旧版本解释器的构建产物在新镜像上
    # 可能取不到，矩阵会随机挂掉。钉一个明确的版本标签，让失败可复现。
    runs-on: ubuntu-22.04
    strategy:
      fail-fast: false
      matrix:
        # 这里必须包含**文档里声称支持的最低版本** ——
        # 否则那句「支持 X+」只是没人验证过的断言。
        python-version: ["3.8", "3.9", "3.12"]
    steps:
      - uses: actions/checkout@v5
        with:
          # 先落到中性目录，下面读出 frontmatter 的 `name` 后再改名。
          path: _checkout

      # 技能规范要求 frontmatter 的 `name` 与「所在目录名」完全一致，而检出目录
      # 固定是**仓库名**（通常不带版本后缀）—— 直接在检出目录里跑校验必然失败。
      # 这是环境差异，不是内容问题，所以先把内容搬进以 `name` 命名的目录，
      # 后续步骤一律在那里执行。直接 `git clone` 的人会遇到同一个坑。
      - name: 按 SKILL.md 的 name 建立同名目录
        id: layout
        run: |
          set -eu
          name="$(python - <<'PY'
          import pathlib, re
          front = pathlib.Path("_checkout/SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
          m = re.search(r"^name:[ \t]*([a-z0-9-]+)[ \t]*$", front, re.M)
          if not m:
              raise SystemExit("SKILL.md 的 frontmatter 里读不到 name")
          print(m.group(1))
          PY
          )"
          mv _checkout "$name"
          echo "skill_dir=$name" >> "$GITHUB_OUTPUT"

      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python-version }}

      - name: 结构规范自检
        working-directory: ${{ steps.layout.outputs.skill_dir }}
        run: python scripts/selfcheck.py . --quiet

      - name: 脚本语法检查
        working-directory: ${{ steps.layout.outputs.skill_dir }}
        run: python -m compileall -q scripts

      - name: 单元测试
        working-directory: ${{ steps.layout.outputs.skill_dir }}
        run: python -m unittest discover -s tests -v
```

**两条设计意图值得写进注释**（这样下一个人不会把它「顺手优化掉」）：

- 矩阵里包含**声称支持的最低版本** ⇒ 那句声明由 CI 自己证明。
- 检出后先改名的步骤 ⇒ 保住「`name` == 目录名」这条命名纪律，
  而不是为了让 CI 变绿就把自检规则放宽。

## 三、发布工作流模板

```yaml
name: release

# 推 tag 时自动建 Release，并附上「解压即可用」的 zip。
# zip 用 `git archive` 从 tag **现算**，不手工打包 —— 见第四节。
#
# zip 的顶层目录名取该 tag 的 SKILL.md 里的 `name`。这点很关键：
# 平台为 tag 自动生成的 "Source code" 归档，顶层目录是 `{仓库名}-{tag}`，
# 分隔符是**点号**；而 `name` 与目录名要求用连字符，两者对不上。
# 直接下载自动归档再手工改名很容易漏。

on:
  push:
    tags:
      - "*"
  workflow_dispatch:
    inputs:
      backfill:
        description: "填 all 才会执行：为全部历史 tag 补建 Release"
        required: false
        default: ""

permissions:
  contents: write

concurrency:
  group: release
  cancel-in-progress: false

jobs:
  release:
    name: 建 Release 并附 zip
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v5
        with:
          # 回填要能枚举全部 tag；同时要能读到主分支上的 CHANGELOG
          fetch-depth: 0

      - name: 确定要发布的 tag
        id: plan
        run: |
          set -eu
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            if [ "${{ inputs.backfill }}" != "all" ]; then
              echo "未传 backfill=all，本次不做事。"
              echo "tags=" >> "$GITHUB_OUTPUT"
              exit 0
            fi
            tags="$(git tag --list | sort -V | tr '\n' ' ')"
          else
            tags="${{ github.ref_name }}"
          fi
          echo "tags=$tags" >> "$GITHUB_OUTPUT"

      - name: 逐个建 Release（已存在则跳过）
        if: steps.plan.outputs.tags != ''
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -eu
          # CHANGELOG 统一从**默认分支**取 —— 它是唯一含全部版本小节的副本：
          # 早期 tag 可能根本没有 CHANGELOG，个别副本也可能被截断过。
          git fetch --quiet origin main
          git show FETCH_HEAD:CHANGELOG.md > /tmp/CHANGELOG.md

          LATEST="$(git tag --list | sort -V | tail -n1)"
          for TAG in ${{ steps.plan.outputs.tags }}; do
            [ -n "$TAG" ] || continue
            # 幂等：已存在就跳过，重复触发不会报错
            if gh release view "$TAG" >/dev/null 2>&1; then
              echo "跳过（已存在）：$TAG"; continue
            fi
            NAME="$(git show "$TAG:SKILL.md" | sed -n 's/^name:[[:space:]]*//p' | head -n1)"
            [ -n "$NAME" ] || { echo "!! $TAG 读不到 name" >&2; exit 1; }
            ZIP="/tmp/$NAME.zip"
            git archive --format=zip --prefix="$NAME/" -o "$ZIP" "$TAG"
            # 逐文件与 tag 对齐，不一致就让发布失败（脚本见第四节）
            python3 "$GITHUB_WORKSPACE/.github/check_zip.py" "$ZIP" "$NAME" "$TAG"
            # 小节映射：早期版本可能没有同名小节
            awk -v want="## $TAG" '$0 == want { f = 1; next } f && /^## / { exit } f' \
              /tmp/CHANGELOG.md > /tmp/section.md
            [ -s /tmp/section.md ] || echo "本版本无独立小节，完整记录见 CHANGELOG。" > /tmp/section.md
            FLAG="--latest=false"; [ "$TAG" = "$LATEST" ] && FLAG="--latest"
            gh release create "$TAG" "$ZIP" --title "$TAG" \
              --notes-file /tmp/section.md "$FLAG"
          done
```

### 回填的三个要点

- **幂等**（已存在则跳过）⇒ 可以重复触发，不需要先删后建。
- **Release 的时间戳取自各自 tag 的提交时间** ⇒ 回填**不会**抢走 Latest；
  `/releases/latest` 会自动落在最新版，**不需要**额外设置 `make_latest`。
- 只在**手动触发且传了开关**时才做事，避免误触发。

### ⚠️ 新仓库首次推送：tag 事件可能被丢弃

**现象**：同一次 `git push` 把主分支与多个 tag 一起推上去。分支正常触发了校验工作流
（并跑绿），但**发布工作流连一条运行记录都没有**，一个 Release 也没建。

**根因**：这是**首次推送**时的竞态 —— 当分支与 tag 出现在同一次 push 里时，
tag 的 push 事件可能在该工作流**完成注册之前**被处理，于是事件被丢弃。
工作流本身是好的（事后查是 `active`），不是配置错误，也不是权限问题。

⇒ **判读纪律**：「新仓库首次推送后 Release 一个都没出现」时，**第一个怀疑对象是
「分支与 tag 是同一次推的」**，而不是权限或 YAML 语法。

**处置**：用工作流自己的**回填入口**补一次，不必改任何配置：

```
POST /repos/{owner}/{repo}/actions/workflows/release.yml/dispatches
     { "ref": "<默认分支>", "inputs": { "backfill": "all" } }
```

回填是幂等的，一次就把缺的 Release 全部补齐。这也是「**先别把回填开关删掉**」的理由：
它平时是历史补档工具，关键时刻是这条竞态的解法。

### ⚠️ 不可变发布要在第一个 Release **之前**开

不可变发布**只保护开启之后创建**的 Release，**不追溯**。所以：

```
先打开开关  →  再发第一个 Release
```

反过来（先建 Release、后开开关）的实测后果是：已存在的 Release 全部仍是
`immutable: false`，想把它们纳入保护**只有删掉重建一条路**。
重建本身是无损的（资产可由 tag 现算、digest 不变），但完全没有必要多这一道。

⇒ 这条要写进发布前的检查清单，并且**要让第一个人就知道** ——
第一个 Release 往往是 CI 自动建出来的，那时人还没想到这个开关。

## 四、发布物必须可由 tag 重现

**核心做法**：资产由 `git archive --format=zip --prefix=<该 tag 的 name>/ -o <输出> <tag>`
**从 tag 现算**。任何人执行同一条命令，都能得到一个**逐文件内容相同**的包。

### 口径必须写准：能承诺「逐文件内容相同」，不能承诺「逐字节相同」

zip 的**条目时间戳取构建机器所在时区的时间**，同一个 tag 在 UTC 与 GMT+8 打出的容器字节不同。
实测差异数是 **条目数 × 2**（local file header 与 central directory 各一处），
且因为时区差是整小时，只改到时间字段的**高字节**，所以每个字段只差 1 字节
—— 「差异数恰好是条目数的 2 倍」这个特征很好认。

⇒ **断言必须打在内容层**（逐文件 Git 对象指纹），它天然与时区、行尾都无关。
把断言打在 zip 文件哈希上，会得到一个**随机变红**的流水线。

**补充一条实测口径**：同一构建环境里，同一个 tag 前后两次构建（间隔几分钟）
产出的 zip **digest 完全相同**。所以准确表述是「**同一环境可复现、跨时区不可复现**」，
而不是「不可复现」—— 前者是能力，后者只是那条要避开的坑。

### 资产命名的一个取舍（写明白，别让人以为是漏了）

工作流里 `ZIP="/tmp/$NAME.zip"` 的结果是：**每个 Release 的资产都叫同一个名字**
（= 包名）。好处是解压出来的目录名直接合规、放进去就能用；
代价是**下载之后从文件名分不出是哪个版本**。

这是**有意取舍**，不是疏忽。若将来要求文件名带版本号，改这一行即可
（解压后的顶层目录名仍取包名，命名三条一致性不受影响）。

### 另一个静默漂移：行尾转换

只被 `.gitattributes` 的 `* text=auto` 覆盖、**没有显式 `eol`** 的文件，
`git archive` 会按客户端的 `core.eol`（默认跟随平台）转换行尾 ⇒
**同一个 tag 在不同平台上打出两个内容不同的包**。

- **修法**：给每个文件类型都**显式**声明 `eol=lf`，别指望 `* text=auto` 兜底。
- 症状很好认：与 tag 的 blob 逐字节比对，差异**恰好只有那几个没写 `eol` 的文件**。

### 一致性自检脚本（放进工作流，不一致就让发布失败）

```python
import hashlib, subprocess, sys, zipfile

zip_path, name, tag = sys.argv[1], sys.argv[2], sys.argv[3]
with zipfile.ZipFile(zip_path) as zf:
    entries = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}

tops = {n.split("/")[0] for n in entries}
assert tops == {name}, "zip 顶层目录不唯一：%s" % sorted(tops)
assert name + "/SKILL.md" in entries
assert not any(".git/" in n for n in entries), "zip 里混进了 .git"
assert not any("__pycache__" in n for n in entries), "zip 里混进了缓存"

def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

tree = subprocess.run(["git", "ls-tree", "-r", "--full-tree", tag],
                      capture_output=True, text=True, check=True).stdout
expected = {}
for line in tree.splitlines():
    meta, path = line.split("\t", 1)
    expected[path] = meta.split()[2]

got = {n.split("/", 1)[1]: blob_sha(d) for n, d in entries.items()}
assert not (set(expected) ^ set(got)), "文件集合不一致"
bad = sorted(p for p in got if got[p] != expected[p])
assert not bad, "内容与 tag 不一致：%s" % bad[:8]
print("zip 自检通过：%d 个条目，与 tag %s 逐文件一致" % (len(entries), tag))
```

> **取数陷阱**：`git archive` 会为**目录**也写入条目，所以
> **zip 条目数 = 文件数 + 目录数**。拿它和别的工具打的包对比时，条目数不会相等
> 而内容仍可能完全相同 —— 比对时只取**不以 `/` 结尾**的条目，别把条目数当一致性判据。

## 五、`actions/*` 的大版本纪律

工作流里 `actions/*` 的大版本决定它跑在哪个运行时上。停在旧大版本会带一条运行时弃用警告：

- 它是 **warning 级**，不影响结论 —— 所以很容易被忽略；
- 但运行时会**按时变成失败**，所以不能长期不管。

**修法是升大版本，但取「刚好消除该警告的最小大版本」：**

1. **现查最新版**（`GET /repos/{owner}/{repo}/releases`，公开仓库免认证可读）——
   **别照抄搜索到的文章**，那上面的版本往往已经过期。
2. **读 release notes 原文找破坏性变更**，据此决定停在哪一档。
   尤其是「凭据持久化方式」「对 fork PR 的检出限制」这类变更，
   若工作流依赖检出动作的默认行为（例如后续要执行 `git fetch`），就会踩到。
3. **判据**：当工作流只用到 `path` / `fetch-depth` / `python-version` 这类最基础的输入时，
   **升一档、不跳档**。在无法本地复现 CI 的前提下，最小行为差就是最小风险。
4. **把这些理由写进工作流的注释** —— 下次再被弃用时照办即可，不用重新论证。

> 附带一个容易误判的现象：弃用清单是按 action 静态清单里的运行时字段生成的。
> 所以即使已经通过环境变量真正跑在新运行时上，清单里**仍然会列出它们**。
> **别据此判断自己没升级** —— 判断升级是否生效，看的是**新一次运行的注解数**是否为 0。

### 为什么会「按时变成失败」——一次真实的时间表

运行时的换代是有节奏的，而不是某天突然坏掉。一轮实测记录下来的节点长这样：

| 里程碑 | 含义 |
|---|---|
| 旧运行时进入维护终止 | 警告开始出现，但**不影响结论** |
| 执行器默认切到新运行时 | 停在旧 action 的步骤会**在没测过的运行时上跑** |
| 执行器**移除**旧运行时二进制、兼容开关失效 | 从这一刻起，**警告变成失败** |

两件事由此推出：

1. **这条警告不能按「不影响结论」处理。** 它是一条有到期日的债。
2. **要在「移除旧二进制」之前修完** —— 到了那一步，工作流会在你没改动任何东西的情况下变红，
   而且第一反应会去怀疑刚改的那次提交。

**判断某个 action 会不会被点名**，看它的 release notes 里哪一版把运行时升上去的；
**判断自己修好了没有**，看**新一次运行**的注解数是否为 0（不是看弃用清单里还有没有它）。

## 六、验收 CI 修复：重放 + 对照

**「本地跑一遍是绿的」不是验收。** 本地绿、CI 红正是踩过的坑。正确做法：

1. **用 YAML 解析器取出那一步的真实脚本**（顺带验证 YAML 合法、块标量的缩进没被吃掉）；
2. 在临时工作区**按 `path:` 建出检出目录**，把那段脚本原样跑一遍；
3. 核对输出变量与目录布局（原名目录是否消失、同名目录是否就位）；
4. 在生成的同名目录里跑**后续各步**；
5. **同时留一个「旧行为」对照组**，确认它仍然失败 —— 这才能证明你修在点上，
   而不是碰巧让别的什么东西变了。

最后用 API 查线上真的转绿了，并按第七节的口径读。

## 七、判断 CI 现状只看最新一次运行

**历史运行是只读快照**：GitHub 不重算历史 run，修好之后那次失败**仍显示红色**，
旧运行上的注解也**不会**因为修好而消失。

判断现状只认三件事：

1. **最新一次 run 的 `conclusion`**（按 `created_at` 排序取最后一条，别只看列表第一屏的观感）；
2. 它的 **`head_sha` 是否在当前主分支的祖先链上** —— 历史被 `amend` 重写过的提交会变成
   **孤儿对象**：不在主分支上，但运行记录仍留着，于是出现「一个不属于当前代码的失败」。
   判据：`git merge-base --is-ancestor <sha> main` 与 `git rev-list --count <sha>..main`；
3. **同一内容是否已有过一次 `success`**。

⚠️ **`conclusion=None` 表示「还没跑完」，不是失败** —— 刚推完去看，拿到的必然是这个值。
它既不能算红叉，也不能算通过（**同一次里别的检查可能已经通过**，那说明只是流水线没走完）。
等它结束再读一次。「已结束却没有结论」（`status=completed` 且 `conclusion` 为空）
是另一回事，那是真异常。

**关于那个红叉该不该修**：若它的提交已是孤儿、当前主分支最新一次是绿的、
且仓库徽章（取默认分支最新 run）显示通过 ⇒ **它对外零影响，属于历史记录而不是待办**。
唯一能让它消失的办法是**删掉那一次运行**（运行页右上 `…` → `Delete run`，
或调 `DELETE /repos/{owner}/{repo}/actions/runs/{id}`，需仓库 admin 权限）——
代价是得再建一个令牌，收益只是视觉。**建议留着**：它正是「同轮 `amend` 重写 →
平台留下孤儿运行」这个现象的实证案例，比任何说明都直观。
