# 安装指南

> 目标:让「谁来干什么」的自动路由在你自己的 ZCode 上跑起来。
> 全程约 20 分钟，分五步，**每一步都能自己验证有没有生效**。
> 前三步做完就能用，后两步是加固。**注意：包里的 `model:` 全是占位符，必须先换成你自己的标识**（第二步）。

前置要求:装了 ZCode；一个终端（用来敲命令看日志）；心里有数你的模型池里谁便宜、谁强、谁有视觉。

所有文件都在你自己的用户目录下，**不需要管理员权限**。

---

## 第一步:放两个审查角色

先别一次装全套。两个角色就够跑通审查这一段。

```bash
mkdir -p ~/.zcode/agents
cp agents/first-reviewer-a.md agents/first-reviewer-b.md ~/.zcode/agents/
```

> **复制完先别验证——包里所有 `model:` 都是占位符（`<your-provider>/…`），原样是跑不起来的。**
> 先按第二步把它们换成你自己机器上的标识，再回来做下面的验证。

**换完模型后，怎么确认生效了**

1. **重开一个会话**（改角色定义要新会话才生效）。
2. 让它列一下可用角色 —— 应该能看到 `first-reviewer-a` 和 `first-reviewer-b`。
3. 直接派一个试试:`用 first-reviewer-a 审一下这段文字`。它应该回一份带 P0/P1/P2 分级的报告，
   并且**用的是你刚填的那个模型**（这点要核，别只看它有回话）。

**看不到这两个角色？** ZCode 只加载 `~/.zcode/agents/` 下的 `.md` / `.markdown` 文件，
文件名必须和文件里的 `name:` 一致。检查大小写和扩展名。

---

## 第二步:换模型 —— 最关键,也最容易搞错

打开 `~/.zcode/agents/first-reviewer-a.md`:

```yaml
name: "first-reviewer-a"
model: "<your-provider>/GLM-5.4"
thoughtLevel: max
maxTurns: 100
injectAgentsMd: false
memory: user
```

**`model:` 这一行，决定了谁来做审查。**

> 仓库里所有 `model:` 都是占位符 `<your-provider>/<模型名>`，**原样复制是跑不起来的**。
> `<your-provider>` 在你机器上可能是 `account:<账号名>` 这样的账户前缀，也可能是一段 provider UUID，
> 以你自己 ZCode 里能选到的值为准（查法见 README「怎么查自己的模型标识」）。

要点：

- **改这一行 = 换人。** 不要改提示词里的模型名 —— 路由 100% 由这个字段决定，
  在提示词里写「你是 GLM-5.4」完全不起作用，只会误导后来读配置的人。
- **两个成员要挑不同家族的。** 同家族 = 共享同一套思维盲区，审出来的东西可能和你自己写的一样错。
- **`maxTurns` 写着「兜底 4」，但它不是硬限制。** 取值优先级是角色自己的 `maxTurns` →
  全局 `subagents.maxTurns` → 4；不过这个值只被写进子运行时的配置，当前版本不拿它掐断执行
  （实测：没写 `maxTurns` 的默认子智能体单轮跑完 300+ 次模型请求）。角色文件里的数字是**意图声明**，
  写清这个岗预期能跑多长；真会打断长任务的是上下文预算和额度。别把它当保险丝。
- **`injectAgentsMd: false`** 表示不把全局规则文档整份塞进每个子智能体。规则越长，
  每次派发的固定开销越大；改成任务书给路径、按需读更便宜。

`model:` 该填什么？见 [README「怎么查自己的模型标识」](README.md#怎么查自己的模型标识)。
**这一步必须按你自己的机器改，直接沿用我文件里的那个值，在你那边可能选不到模型。**

**怎么确认生效了**:重开会话，派一次审查，看回话用的模型对不对。

---

## 第三步:加轮换,让同一任务拿到两个视角

```bash
cp hooks/review_role_rotation.py ~/.zcode/agents/
```

把 `config-hooks.snippet.json` 里 `PreToolUse` 的那条（`review_role_rotation.py`）
合并进 `~/.zcode/cli/config.json`。如果这个文件还没有 `hooks` 键，整段加进去；
已经有了就只加 hook 条目，**别把别的配置覆盖掉**。

**怎么确认生效了**

1. 重开会话。
2. 在**同一个会话里**连派两次 `first-reviewer-a`。
3. 看日志:

```bash
tail -5 ~/.zcode/agents/.review-rotation.log
```

看到箭头两边角色不一样，就说明在轮换了：

```
rewrite | role=first-reviewer-a->first-reviewer-b bg_injected=True
rewrite | role=first-reviewer-b->first-reviewer-a bg_injected=True
```

顺带说明:这个 hook 还会把 `run_in_background` 设成 `true`，审查在后台跑、不阻塞主会话。
某些「低峰/闲时」模式不接受后台派发，那种场景显式传 `run_in_background: false` 走前台。

---

## 第四步:加闸门,防止审查链被悄悄跳步

```bash
cp hooks/review_dispatch_guard.py ~/.zcode/agents/
```

把 snippet 里 PreToolUse 的 `review_dispatch_guard.py pre`，以及
`PostToolUse` / `PostToolUseFailure` 两条 `post` 都合并进配置
（`post` 负责登记 agentId→角色，后续 resume 时才知道该拦谁）。

**怎么确认生效了**

派一次没有依据的审查，应该被拒：

```bash
echo '{"tool_name":"Task","session_id":"t","tool_input":{"subagent_type":"first-reviewer-a","prompt":"审一下"}}' \
  | python3 ~/.zcode/agents/review_dispatch_guard.py pre
echo "退出码 $?"
```

退出码 `2` = 按预期拒绝（stderr 里有一行怎么补的指引）。

带上依据就该放行：

```bash
echo '{"tool_name":"Task","session_id":"t","tool_input":{"subagent_type":"first-reviewer-a","prompt":"首审（链中第一环，无上一状态行）。"}}' \
  | python3 ~/.zcode/agents/review_dispatch_guard.py pre
echo "退出码 $?"
```

退出码 `0` = 放行，正常。

---

## 第五步:补执行岗、终审，加自愈

```bash
cp agents/impl-worker.md agents/vision-reader.md agents/doc-writer.md agents/reviewer.md ~/.zcode/agents/
cp hooks/heal_frontmatter.py ~/.zcode/agents/
```

再把 snippet 里 `SessionStart` 那条（`heal_frontmatter.py`）合并进配置。

四个角色：

| 角色 | 干什么 | 模型怎么挑 |
|---|---|---|
| `impl-worker` | 按规格写码、跑命令、批量改文件 | **两个快模型按需切** —— 这里是回合数的大头 |
| `vision-reader` | 判图、看录屏 | 有视觉输入的 |
| `doc-writer` | 中文成稿、润色 | 中文好的 |
| `reviewer` | 异构终审 | **和产出方、一审都不同家族的** |

`impl-worker` 的 `model:` 建议先填一个额度宽的，需要在通道之间切换时改那一行。
它和审查岗的区别是:**换模型不影响判断质量，只影响成本和通道稳定性**。

`reviewer` 是整套东西的关键:它是唯一一个「保证和上游都不同源」的角色。

审查角色文件里还带一条「整体判断前置」：报告第一节先复述目标与真实场景，判方案是否成立 / 有无更优路径，再进问题清单；方向不成立时状态行首跳=用户裁决，结构性换方案只记待拍板、不产生首跳。代码 diff / B 通道规格对照 / 测试报告写 `n/a`（无上游方案审的大改除外）。

`heal_frontmatter.py` 的效果:面板保存丢了 `memory` / `thoughtLevel`，
下次开会话自动补回来。手动检查（只报告不改动）:

```bash
python3 ~/.zcode/agents/heal_frontmatter.py --check
```

---

## 收尾:跑一遍自测

在仓库目录里:

```bash
python3 test/smoke.py
```

**30 项全绿才算装对了。** 它不碰你的真实配置——角色目录、状态、日志全部重定向到临时目录。

---

## 一份最小可用的审查任务书

闸门认「上一份报告状态行原文」这个标题，所以任务书里要有它：

```markdown
# 审查任务书:<被审产物名>

## 通道判定
重量级（进 repo / 交付他人 / 定稿 / 自产方案）

## 上一份报告状态行原文
首审（链中第一环，无上一状态行）。

## 被审范围
<文件路径 / 范围说明>

## 必查项
1. …  2. …  3. …

## 报告落盘
<绝对路径,本轮开工时该文件不存在>

## 审查任务
<要审什么、审到什么程度>
```

复审轮把「首审…」那行换成上一份报告的真实状态行，例如：

```
〔下一跳:修复审(主会话修复本轮发现后派 first-reviewer-a;之后→终审;闭案条件=0P0/0P1)〕
```

写任务书的两条规矩（来自实际踩坑）：

1. **给路径,不给内容。** 让审查的人自己读文件，你只写路径、参数、验收标准。
2. **产物落盘,只回摘要。** 报告写文件，返回只给摘要加路径。不然中间过程全挤回主会话。

---

## 常见问题

**Q:提示找不到角色？**
A:文件名要和文件里的 `name:` 完全一致，扩展名 `.md` / `.markdown`，放在 `~/.zcode/agents/`。改完重开会话。

**Q:改了 `model:` 没生效？**
A:角色定义要**新开会话**才生效；同一会话里还是老的。

**Q:轮换日志里两个角色一直是同一个？**
A:检查 `~/.zcode/agents/.review-rotation.json` 是否被写坏；删掉它，会自动重建（只丢历史轮换记录）。

**Q:闸门把正常派发拦了？**
A:任务书里缺「上一份报告状态行原文」那段，或段里只是提了一句「状态行」而没有真正的声明行。
闸门认这些开头:`〔下一跳`、`〔状态`、`〔本轮状态`、`状态：`、`首审`（可带 `>`/`-`/`#`/反引号/ASCII 角色标签前缀）。
这条清单只适用于**任务书路径**;如果依据是直接内联写在 prompt 里的,只认 `〔下一跳` 和行首 `首审` 两种。

**Q:能只装轮换、不装闸门吗？**
A:能。轮换解决「谁来审」，闸门解决「有没有跳步」。两者独立，但跳步是更难发现的那种失效，建议都装。

**Q:面板改了配置后字段不见了？**
A:这是面板「重建式保存」的已知行为。装上 `heal_frontmatter.py` 后，下次开会话会自动补回。
根治办法是**手写 markdown 文件**，面板只当取值器用。
