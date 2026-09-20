# Duty-Assistant
The assistant of the operation duty officer involves 10 types of operation duty and regular work records.

Core package: `records_kit` (see `docs/design.md`).

## 文档入口

| 文档 | 说明 |
|---|---|
| `docs/design.md` | 核心设计文档（v1.5 审定稿 + 修订 A/B/C） |
| `docs/shell-integration-guide.md` | **壳层对接说明（完整版，M3 交付物）** |
| `docs/error-codes.md` | 错误码与告警码总表 |
| `docs/dsl-gap-list.md` / `docs/dsl-gap-verify-report.md` | DSL 缺口清单（M0）与核验报告 |
| `docs/dsl-grammar-ext-proposal.md` | 文法扩展提案（7 项拍板落文） |
| `docs/m3-preflight-impact-assessment.md` | M3 预热影响评估 |
| `docs/trend-caliber-proposal.md` | trend 口径收敛提案 |
| `docs/repository-checks.md` | 仓库检查说明 |

## 开发环境搭建

要求 Python ≥ 3.12（设计文档 §4 环境假设；开发机为 3.14 亦可）。包为零运行时依赖，测试用 pytest。

以下命令均在仓库根目录执行；路径从项目根解析（协作约束见 `AGENTS.md`）。

### 方式一：uv（推荐）

```bash
# bash (Linux/macOS/Windows git-bash)
uv venv .venv                        # 创建虚拟环境
uv pip install -e ".[test]"          # 可编辑安装 + pytest
uv run pytest                        # 跑测试
```

```powershell
# Windows PowerShell
uv venv .venv
uv pip install -e ".[test]"
uv run pytest
```

### 方式二：系统 python + pip

```bash
# bash (含 Windows git-bash)
python3 -m venv .venv
source .venv/Scripts/activate        # Windows git-bash 下激活
# source .venv/bin/activate          # Linux/macOS 用这行
python -m pip install -e ".[test]"
python -m pytest
```

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest
```

### 说明

- `.venv/` 已被 `.gitignore` 忽略，不入库。
- 安装采用 `src/` 布局的可编辑模式（`pip install -e .`），`import records_kit` 即用工作区源码。
- CI 只覆盖仓库检查（gitleaks 等），业务可用性以测试与评审为准（`AGENTS.md`）。
