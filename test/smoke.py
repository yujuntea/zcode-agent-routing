#!/usr/bin/env python3
"""零依赖自测:验证三个 hook 的关键行为。

不碰你的真实配置——所有状态/日志都重定向到临时目录,角色目录也是复制出来的副本。

跑法:
    python3 test/smoke.py

全绿退出码 0;任何一项失败退出码 1。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")
TMP = tempfile.mkdtemp(prefix="zar-smoke-")
PASS, FAIL = [], []


def run(script, args, stdin_obj, env_extra):
    """调用一个 hook,返回 (退出码, stdout)。"""
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.run(
        [sys.executable, os.path.join(HOOKS, script)] + args,
        input=json.dumps(stdin_obj, ensure_ascii=False),
        capture_output=True, text=True, env=env,
    )
    return p.returncode, p.stdout, p.stderr


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def dispatch(role, prompt="p", session="s1", **kw):
    ti = {"subagent_type": role, "prompt": prompt}
    ti.update(kw)
    return {"tool_name": "Task", "session_id": session, "tool_input": ti}


def main():
    rr = {"RR_STATE": os.path.join(TMP, "rr.json"), "RR_LOG": os.path.join(TMP, "rr.log")}
    gd = {"REVIEW_DISPATCH_REG": os.path.join(TMP, "reg.json"),
          "REVIEW_DISPATCH_LOG": os.path.join(TMP, "guard.log")}

    print("\n[1] 轮换 hook:同会话内两个成员交替")
    code, out, _ = run("review_role_rotation.py", ["pre"], dispatch("first-reviewer-a"), rr)
    first = json.loads(out)["hookSpecificOutput"]["updatedInput"]["subagent_type"]
    check("首次派发被改写为新会话起始成员", code == 0 and first in ("first-reviewer-a", "first-reviewer-b"))
    code, out, _ = run("review_role_rotation.py", ["pre"], dispatch("first-reviewer-a"), rr)
    second = json.loads(out)["hookSpecificOutput"]["updatedInput"]["subagent_type"]
    check("第二次派发换到另一个成员", first != second, f"{first} → {second}")

    print("\n[2] 轮换 hook:显式前台不被后台覆盖")
    code, out, _ = run("review_role_rotation.py", ["pre"],
                       dispatch("first-reviewer-a", run_in_background=False), rr)
    bg = json.loads(out)["hookSpecificOutput"]["updatedInput"].get("run_in_background")
    check("run_in_background=false 原样保留", bg is False, f"实际 {bg}")

    print("\n[3] 轮换 hook:未传后台时默认注入 true")
    code, out, _ = run("review_role_rotation.py", ["pre"], dispatch("first-reviewer-b"), rr)
    bg = json.loads(out)["hookSpecificOutput"]["updatedInput"].get("run_in_background")
    check("默认注入 run_in_background=true", bg is True, f"实际 {bg}")

    print("\n[4] 轮换 hook:REVIEW-ROLE-FIXED 固定成员")
    rr2 = {"RR_STATE": os.path.join(TMP, "rr2.json"), "RR_LOG": os.path.join(TMP, "rr2.log")}
    run("review_role_rotation.py", ["pre"], dispatch("first-reviewer-a"), rr2)
    code, out, _ = run("review_role_rotation.py", ["pre"],
                       dispatch("first-reviewer-b", prompt="REVIEW-ROLE-FIXED", run_in_background=False), rr2)
    check("固定标记时不改写角色(无输出)", out.strip() == "", out.strip()[:80])

    print("\n[5] 轮换 hook:非审查角色零影响")
    code, out, _ = run("review_role_rotation.py", ["pre"], dispatch("impl-worker"), rr)
    check("impl-worker 派发不被改写", out.strip() == "", out.strip()[:80])

    print("\n[6] 两个 hook:异常输入一律放行(fail-open)")
    env = dict(rr); env.update(gd)
    code1, _, _ = run("review_role_rotation.py", ["pre"], "not-a-dict", env)
    p = subprocess.run([sys.executable, os.path.join(HOOKS, "review_dispatch_guard.py"), "pre"],
                       input="garbage", capture_output=True, text=True,
                       env={**os.environ, **env})
    check("轮换 hook 遇到怪输入退出码 0", code1 == 0)
    check("闸门遇到怪输入退出码 0", p.returncode == 0)

    print("\n[7] 闸门:缺进场依据要拒绝")
    code, _, err = run("review_dispatch_guard.py", ["pre"], dispatch("first-reviewer-a", prompt="审一下"), gd)
    check("缺依据退出码 2", code == 2, f"实际 {code}")
    check("拒绝原因写到 stderr", "REVIEW-DISPATCH" in err)

    print("\n[8] 闸门:合法依据要放行")
    code, _, _ = run("review_dispatch_guard.py", ["pre"], dispatch("first-reviewer-a", prompt="首审（链中第一环，无上一状态行）。"), gd)
    check("「首审」声明放行", code == 0, f"实际 {code}")
    code, _, _ = run("review_dispatch_guard.py", ["pre"], dispatch("reviewer", prompt="〔下一跳:K3 全量终审(…;闭案条件=0P0/0P1)〕"), gd)
    check("〔下一跳 状态行放行", code == 0, f"实际 {code}")

    print("\n[9] 闸门:任务书路径式依据")
    tb_dir = os.path.join(TMP, "tb"); os.makedirs(tb_dir, exist_ok=True)

    def write_tb(name, decl):
        p = os.path.join(tb_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 审查任务书\n\n## 上一份报告状态行原文\n" + decl + "\n\n## 审查任务\n审。\n")
        return p

    cases = [
        ("taskbook-good.md", "〔下一跳:修复审(主会话修复后派 first-reviewer-a;之后→K3 全量终审;A 通道)〕", 0, "裸状态行"),
        ("taskbook-quoted.md", "`〔下一跳:修复审(…;闭案条件=0P0/0P1)〕`", 0, "反引号包裹"),
        ("taskbook-statusfirst.md", "〔状态:已改待验〕〔下一跳:K3 收口终验(…;闭案条件=0P0/0P1)〕", 0, "状态行在前"),
        ("taskbook-firstreview.md", "首审（链中第一环，无上一状态行）。", 0, "首审声明"),
        ("taskbook-template.md", "这里写一个检查清单：〔下一跳…〕", 2, "模板占位/清单句"),
        ("taskbook-mention.md", "状态行格式是〔下一跳:X(之后→Y;闭案条件=Z)〕", 2, "仅提及格式"),
    ]
    for fname, decl, expect, label in cases:
        path = write_tb(fname, decl)
        code, _, _ = run("review_dispatch_guard.py", ["pre"],
                         dispatch("first-reviewer-a", prompt=f"任务书：{path}"), gd)
        check(f"{label} → {'放行' if expect == 0 else '拒绝'}", code == expect, f"实际退出码 {code}")

    print("\n[10] 闸门:post 模式登记 agentId→角色")
    post = {"tool_name": "Task", "session_id": "s1",
            "tool_input": {"subagent_type": "first-reviewer-a", "prompt": "首审"},
            "toolResponse": {"agentId": "agent_smoke0001"}}
    run("review_dispatch_guard.py", ["post"], post, gd)
    reg = json.load(open(gd["REVIEW_DISPATCH_REG"], encoding="utf-8"))
    check("登记表写入成功", reg.get("agent_smoke0001", {}).get("role") == "first-reviewer-a", json.dumps(reg, ensure_ascii=False)[:120])

    print("\n[11] 自愈 hook:补齐丢失字段、幂等、不动结构异常的件")
    agents = os.path.join(TMP, "agents"); os.makedirs(agents, exist_ok=True)
    heal_env = {"ZC_AGENTS_DIR": agents, "ZC_BACKUP_DIR": os.path.join(TMP, "bak")}
    victim = os.path.join(agents, "first-reviewer-a.md")
    with open(victim, "w", encoding="utf-8") as f:
        f.write('---\nname: "first-reviewer-a"\nmodel: "x/y"\nmaxTurns: 100\n---\n\n# 角色\n正文不该被改。\n')
    broken = os.path.join(agents, "vision-reader.md")
    with open(broken, "w", encoding="utf-8") as f:
        f.write("# 没有 frontmatter\n")
    before_broken = open(broken, encoding="utf-8").read()

    code, out, _ = run("heal_frontmatter.py", [], {}, heal_env)
    body = open(victim, encoding="utf-8").read()
    check("补上 memory 与 thoughtLevel", "memory: user" in body and "thoughtLevel: max" in body)
    check("正文原样保留", "正文不该被改。" in body)
    check("修复后向会话注入提示", "frontmatter 自愈" in out, out[:100])

    code, out2, _ = run("heal_frontmatter.py", [], {}, heal_env)
    check("第二次运行幂等(无输出)", out2.strip() == "", out2.strip()[:100])
    check("结构异常的文件未被改动", open(broken, encoding="utf-8").read() == before_broken)

    print("\n" + "=" * 52)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"失败 {len(FAIL)}/{total}:")
        for name in FAIL:
            print("  - " + name)
        print(f"临时目录保留: {TMP}")
        return 1
    print(f"全部通过 {total}/{total}")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
