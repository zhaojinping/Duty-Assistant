---
name: duty-assistant
description: >
  蓄电池电压测量的安装与日常运行。只交付这一类。
  触发词：安装值班助手、蓄电池电压安装、电压测量安装、Duty-Assistant 安装。
  读者是智能体。用户不懂命令。命令由你执行。所有智能体走同一份步骤。
---

# 蓄电池电压测量 · 安装

本次只做蓄电池电压。跳闸、避雷器、两票等其余记录不在这次安装里。用户问起就说明这次没有。

用户是小白。先把要做的事用白话说完，等用户明确同意，再执行带 `--confirm` 的命令。钉钉扫码必须用户本人用手机点。

## 数据放哪

- Windows：`%LOCALAPPDATA%\DutyAssistant`
- Mac：`~/Library/Application Support/DutyAssistant`
- 不放仓库、桌面、文稿、iCloud、OneDrive。
- 新装使用空账本。不拷贝开发库，不使用开发者的路径、群、人员和表格。

## 先安装这个包

仓库是 src 布局。新机器上还没安装时，直接 `python -m da_core.cli` 会报找不到 `da_core`。

在仓库根目录，用 Python 3.12 及以上执行：

```text
python scripts/bootstrap.py
```

它在仓库里建立 `.venv` 并做可编辑安装。之后的命令都用这个环境里的 Python：

- Windows：`.venv\Scripts\python.exe -m da_core.cli ...`
- Mac：`.venv/bin/python -m da_core.cli ...`

下面示例里的 `python` 都指这个环境。定时任务也会自动用它。

## 每次先体检

```text
python -m da_core.cli health
```

看 JSON。`ready_to_install` 为真才继续问下面的问题。某一项失败，就用该项的 `message` 告诉用户，不要继续写配置。

## 要问用户的话

一次问完，不要甩命令：

1. 这台电脑是不是这个场站唯一写账的电脑。如果另一台已经装过，停下来，这台只看钉钉。
2. 场站中文名。你拟一个短码（字母数字）读给用户，用户同意后再用。
3. 生产群名称。用 `dws chat +chat-search --query 群名 --format json` 查找。多名就列出让用户选。只有一个才记下 `openConversationId`。群名是「APM测试」就再问一次。
4. 待办责任人姓名。用钉钉通讯录查到唯一的人再记下用户编号。重名就列出让用户选。
5. 班长姓名。可以不填。不填就不发私聊。
6. 有几组电池，每组是 2V 还是 12V，多少只。不要用任何场站的现成四组。
7. 到期默认每月 15 日。告诉用户可以改日子，或改成每 30 天。没改就用每月 15 日，起算日用下一个 15 日。
8. 合格范围先用 2V 为 1.85–2.35、12V 为 11.85–13.80。用户不提出新数字就不改。
9. 录入页：按仓库里的 7 行文本（组别、温度、直流系统、浮充电压、测试性质、数据、提交时间）帮用户在 WorkBuddy 免费托管上新建自己的页面。必须本人点发布时，停下来等网址。网址必须是 https，且不能是 `battery-voltage-entry.app.workbuddy.host`。

把上述答案读一遍。用户明确同意后，再安装依赖、再 `init --confirm`。

## 安装依赖

`health` 里缺 Python 或 dws 时：

```text
python -m da_core.cli install-deps
```

把 `steps` 读给用户。同意后再：

```text
python -m da_core.cli install-deps --confirm
```

只装 Python 3.12 和钉钉 dws。装 dws 时如果没有 Node.js，会一并安装。不装 Git、uv、测试工具，也不另装 SQLite。装完如果换了 Python，用新的 Python 再跑一遍 `health`。

## 写入配置

组别写成一个 JSON 文件，例如 `groups.json`：

```json
[{"name": "1号组(18只)", "kind": "12V电池", "count": 18}]
```

`kind` 只能是 `2V单体` 或 `12V电池`。

先不加 `--confirm` 跑一遍，把返回的内容读给用户。用户同意后加上 `--confirm`。同一台电脑再跑会更新配置，不会清空已有记录。另一台电脑会直接拒绝。

```text
python -m da_core.cli init --station-id XP --station-name 香坪风电场 --group-name 香坪运行群 --group-cid <会话编号> --assignee-name 张三 --assignee-id <人员编号> --entry-url https://用户自己的页面 --groups-file groups.json --confirm
```

班长有名字时再加 `--escalate-name` 和 `--escalate-id`。

## 新建钉钉表

```text
python -m da_core.cli create-table
```

告诉用户将新建《蓄电池电压测量记录》，13 列固定，列名不能改。同意后：

```text
python -m da_core.cli create-table --confirm
```

发不出互动卡片不影响安装。文字提醒和待办仍然可用。

## 定时任务

告诉用户：每 5 分钟拉一次录入和群消息，每天 08:30 催办；这台电脑到点要开着。同意后只注册当前这一种系统。解释器必须是上面建好的 `.venv`，不要用没装过本包的系统 Python：

```text
python -m da_core.cli register-tasks --confirm
```

## 更正和作废

先复述是哪一条、原因是什么。用户明确同意后，命令才加 `--confirm`。没有 `--confirm` 时命令不会改账本。

## 禁止

- 把开发者的路径、群、人员、表格或录入网址写进用户配置。
- 在用户电脑上开接收端口。核心只主动拉取。
- 两台电脑同时写同一场站的账。
- 替用户扫码，或替用户修改合格范围。
- 写表没有记录号时对用户说「已入库」。
