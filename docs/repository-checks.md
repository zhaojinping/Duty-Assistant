# 仓库检查说明

本仓库的门禁由 `scripts/repo_checks.py` 统一承担，本地 pre-commit 钩子与 GitHub CI 调用同一入口，检查口径一致。文件检查为 Python 标准库实现、零第三方依赖；测试阶段通过子进程调用 pytest（仅测试需要 pytest，见 `pyproject.toml` 的 `[test]` extra）。

## 首次使用

```text
python scripts/install_hooks.py
```

安装仓库局部钩子（只写本仓库的 `core.hooksPath`，不改全局 Git 配置；已存在不同的 hooksPath 时拒绝覆盖）。之后每次提交前自动运行暂存区检查。

## 检查内容

入口 `python scripts/repo_checks.py`，两阶段：

1. 文件检查（`scripts/repo_guard.py`）
   - 隐私扫描：明文密钥赋值（名字含 key/token/secret/password 等词段，值为 8 字符以上 ASCII 字面量），覆盖三种常见形态——无引号键+引号值（Python/env）、JSON 引号键、无引号 YAML·env 裸标量；另有 token/私钥形态、个人绝对路径（`<盘>:\Users\<名>\`、`/home/<用户>/`、`/Users/<用户>/`）、文档署名（`署名：`/`作者：`/`author:`）。仅按完整占位格式放行：`xxxx-xxxx` 全 x 串、`<...>`、`${...}`、`{{ ... }}`、`your-...` 前缀及 changeme/example 等整词；值中间夹着占位词不豁免。
   - 秘密扫描以 gitleaks 为主力（640+ 内置规则：云厂商、SaaS token、私钥、通用 API key + 熵分析），本地钩子扫描暂存增量，CI 全量扫描全部提交历史；占位符豁免集中在仓库根 `gitleaks.toml`（口径与 repo_guard 一致：仅整值占位放行，夹在真实值中间不放行）。repo_guard 保留为隐私/格式/署名层的补充。
   - 本地产物拦截：`.dev-flow`、`local-private`、`runtime`、`logs` 等目录，`.bak/.log/.tmp/.swp/.pyc` 后缀，文件名含 handover/receipt/交接/回执，以及 `*.local.*` 配置覆盖、`.env*`（`.env.example` 除外）、二进制数据文件（`.docx/.xlsx/.pdf/.db/.zip` 等，需单独审批）。
   - 语法检查：受跟踪 `.py` 文件 `ast.parse`，不执行被扫描模块。
   - 基础格式：`.py`/`.yml` 行尾空白、`.py`/`.md`/`.yml` 末尾换行。`.md` 不查行尾空白（Markdown 双空格换行是合法语法）。
2. 测试执行：**已启用**。`tests/` 目录存在时自动执行 `pytest -q`（全量与 `--staged` 模式都跑，与 duty-ops 同口径——每次提交都验证；优先用仓库 `.venv` 的解释器，无 venv 时用当前解释器，当前解释器无 pytest 时本地钩子会失败，须先 `python -m venv .venv && .venv/Scripts/python -m pip install -e '.[test]'`）；零用例、任一失败或报错均非零退出。CI 先 `pip install -e '.[test]'` 再调本入口，故 pytest 恒可用。

## 全量与暂存区语义

- 全量模式（CI，默认）：检查 `git ls-files` 列出的全部跟踪文件，读工作区内容。
- `--staged` 模式（钩子）：只检查已暂存（`git diff --cached`，ACMR）文件，读暂存区内容（`git show :path`）。工作区已修复但暂存区仍含违规内容时会继续拦截，须重新 `git add`。

路径一律从项目根目录解析，可从任意当前目录调用。

## 边界（如实声明）

- 秘密扫描主体 gitleaks：本地钩子只扫暂存增量，提交历史由 CI 全量兜底；两者口径同源（`gitleaks.toml`）。扫描器缺失或不可执行时钩子 fail-closed（拒绝提交），不是悄悄放行。
- 本地钩子仍可被 `--no-verify` 跳过——客户端钩子原理上无强制力。真正的合并约束在服务端：`main` 已启用分支保护（必须 PR、必须过 `repo-check`、禁强推与删除、`enforce_admins` 开启——管理员直推同样被 `GH006` 拒收）。绕过钩子的提交进不了 `main`，或进了也会被 CI gitleaks 岗当场判红。清理误推需临时撤保护、`--force-with-lease` 回退、再恢复保护。
- CI 与钩子都是保守检查，不能替代人工看 diff；发现泄漏按密钥已泄漏处理（立即吊销轮换，而非删除提交了事——历史里删不掉）。

CI 在 Windows 与 Ubuntu 上跑 Python 3.12/3.13。第三方 Action 钉 SHA。
