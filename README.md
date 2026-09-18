# Duty-Assistant
The assistant of the operation duty officer involves 10 types of operation duty and regular work records.

Core package: `records_kit` (see `docs/design.md`).

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
