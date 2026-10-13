# 快速上手案例

全部案例都在 Windows + PowerShell 下实测通过。先设置三个变量，后文直接用：

```powershell
# 换成你自己的 python 路径（managed 运行时优先，系统 python 亦可）
$PY    = "C:\Users\<你的用户名>\.workbuddy\binaries\python\versions\3.13.12\python.exe"
$RELAY = "$env:USERPROFILE\.workbuddy\skills\codex-relay\scripts\codex_relay.py"
$WS    = "C:\path\to\your\workspace"
```

> macOS / Linux 下 `PY` 用 `python3` 即可，其余命令结构完全一致。
> 在 WorkBuddy 对话里使用时，你不需要手动敲这些命令 —— 直接说
> "用 codex-relay 帮我……" 即可。下面写命令是为了让你理解每一步在做什么。

---

## 案例 1：最小闭环 —— 让 Codex 规划、执行、审查

**场景**：想验证这套协作是否值得用，从最小任务开始。

告诉 WorkBuddy："用 codex-relay 在 $WS 创建一个 hello.txt，内容为 relay ok"。

手工执行等价于：

```powershell
# ① Codex 规划（只读沙箱，约 40-90 秒）
& $PY $RELAY plan --cwd $WS `
  --task "Create a file named hello.txt in the current directory containing exactly the text: relay ok" `
  --out "$WS\.codex-relay\plan.json"

# ② 按计划逐步执行。Codex 在 Windows 上会生成 .ps1 语法，用 PowerShell 跑
#    （注意：用 PowerShell 工具执行，不要用 Bash）
#    ...执行后把输出存到 .codex-relay\step1.out

# ③ 记录真实结果
& $PY $RELAY report --plan "$WS\.codex-relay\plan.json" `
  --step 1 --exit-code 0 --status done `
  --stdout-file "$WS\.codex-relay\step1.out" `
  --artifacts hello.txt `
  --note "wrote hello.txt (8 bytes), hex verified 72-65-6C-61-79-20-6F-6B" `
  --out "$WS\.codex-relay\execution.json"

# ④ 封存：确认没有漏步
& $PY $RELAY collect --plan "$WS\.codex-relay\plan.json" --out "$WS\.codex-relay\execution.json"

# ⑤ Codex 审查（复用规划 session，约 40-90 秒）
& $PY $RELAY review --plan "$WS\.codex-relay\plan.json" `
  --report "$WS\.codex-relay\execution.json" `
  --out "$WS\.codex-relay\review.json" --cwd $WS
```

**实测输出**：`VERDICT: approved`，`findings[0].status = met`，进程退出码 0。

---

## 案例 2：多步任务 + 危险操作确认

**场景**：批量重命名，需要 Codex 拆成可审查的步骤。

```powershell
& $PY $RELAY plan --cwd $WS `
  --task "把 data\raw 下的 120 个 .csv 重命名为 <日期>_<原名>.csv，日期取文件 mtime" `
  --context "目标目录 data\raw；先输出预览清单，确认无误后再实际重命名" `
  --out "$WS\.codex-relay\rename-plan.json"
```

**看计划时重点检查两处**：

- Codex 是否把"预览"和"实际改名"拆成**两个独立步骤**（它应该这么拆）
- 真正执行 `Move-Item` 的那一步，`requires_confirmation` 是否为 `true`

带 `[CONFIRM REQUIRED]` 标记的步骤，**必须先问用户**再执行 —— 这不是流程冗余，
批量改名一旦错了很难逐个回滚。

---

## 案例 3：Codex 审查现有结果（不重新规划）

**场景**：你或别人已经跑完活儿，只想让 Codex 挑毛病。

不跑 `plan`，手工准备两份 JSON 后直接 `review`：

```powershell
# plan.json 可以从上一次 relay 运行里复制，或按 references\protocol.md 手写
# execution.json 至少要有 results[] 和 _relay；没有 sealed 标记会被拦下
& $PY $RELAY collect --plan "$WS\.codex-relay\plan.json" --out "$WS\.codex-relay\execution.json"
& $PY $RELAY review --plan "$WS\.codex-relay\plan.json" `
  --report "$WS\.codex-relay\execution.json" `
  --out "$WS\.codex-relay\review.json" --cwd $WS --fresh
```

`--fresh` 让 Codex 开新会话、不带旧计划偏见 —— 独立审查时更合适。

---

## 案例 4：命中 `needs_revision` 后怎么走

**场景**：审查没过。这是**最需要照章办事**的分支。

Codex 返回 `needs_revision`、退出码 3 时，脚本会直接告诉你下一条命令：

```
==> needs_revision: re-run `plan` with --iteration 2 (cap 3) feeding these findings back as --context.
    Do NOT re-run the failing command by hand and do NOT patch around it.
```

照做：

```powershell
& $PY $RELAY plan --cwd $WS --iteration 2 `
  --task "把 data\raw 下的 120 个 .csv 重命名为 <日期>_<原名>.csv，日期取文件 mtime" `
  --context @"
Codex 对第 1 轮的审查结论：
- step 3: not_met —— 命令用 Get-ChildItem 递归时包含了 data\raw\backup 子目录，
  导致清单多出 40 个文件。必须限制在顶层，不要 -Recurse。
- step 3: unverifiable —— 预览命令没有任何 stdout，无法验证清单正确性。
  请让预览步骤明确输出行数与前 5 行文件名。
"@ `
  --out "$WS\.codex-relay\rename-plan.json"
```

**关键点**：`--iteration` 必须递增（脚本会校验），让 Codex 知道自己是在改方案，
而不是重复劳动。**不要**直接重跑失败命令，**不要**手工改 `review.json`。

迭代到第 3 轮仍不通过时，脚本抛 `MAX_ITERATIONS`，此时停下来找用户，别自己硬撑第 4 轮。

---

## 案例 5：`rejected` —— 停止并上报

Codex 判定"思路本身错了"（比如目标理解偏了、方案不安全）时返回 `rejected`。

此时的正确动作是**停下来**，向用户说明三件事：原始目标是什么、Codex 认为的思路错在哪、
它的 `next_action` 建议。不要自动重规划 —— 目标层面的误解只能由用户澄清。

---

## 案例 6：报告"未验证"而非谎报成功

**场景**：命令退出码 0，但输出为空，你无法确认效果是否达成。

这是最容易翻车的地方。诚实记录：

```powershell
& $PY $RELAY report --plan "$WS\.codex-relay\plan.json" `
  --step 2 --exit-code 0 --status done `
  --stdout-file "$WS\.codex-relay\step2.out" `
  --note "命令退出码 0 且无 stderr，但我没有独立验证内容是否符合预期"
```

Codex 会据此把该步判为 `unverifiable`，而不是因为"退出码 0"就放行。
**退出码 0 不等于成功** —— 把它如实写进 `--note`，审查才有意义。

---

## 常见报错速查

| 报错 | 原因 | 处理 |
|---|---|---|
| `[MODEL_REJECTED]` | 指定了账号不支持的模型 | 去掉 `--model`，让 codex 自行回退 |
| `[CODEX_TIMEOUT]` | 默认 900s 仍没返回 | `--timeout 1800`，或把任务拆小 |
| `[CODEX_TRANSPORT]` | 到 chatgpt.com 的 TLS 断连（脚本已重试 2 次） | 稍后重试；持续出现则查网络 |
| `[EXTRA_RESULTS]` | 报告里有计划外的步骤 | 补进 plan，或删掉多余结果 |
| `[INCOMPLETE_EXECUTION]` | 有步骤没记录 | 补记录，或用 `--status skipped --note "原因"` |
| `[REPORT_NOT_SEALED]` | 没跑 `collect` 就去审查 | 先跑 `collect` |
| `[MAX_ITERATIONS]` | 超过 3 轮 | 停止，找用户 |

---

## 成本预期

完整一轮 = 2 次 Codex 调用（plan + review），约 **2–4 分钟**；
命中 `needs_revision` 则每轮再加 2 次。所有中间产物都在 `.codex-relay\` 下，
可随时翻看 Codex 到底为什么批准或否决。
