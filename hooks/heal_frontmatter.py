#!/usr/bin/env python3
"""heal_frontmatter.py — 子智能体 frontmatter 自愈(建议挂在 SessionStart)

为什么需要它:ZCode 面板保存子智能体时,会用"重建式"序列化——它只回写自己
认识的字段,手写但你面板里没有对应控件的字段会被静默丢弃。实测最容易丢的两个:

  - memory      面板 host 层压根没有这个字段的代码,必然丢
  - thoughtLevel 面板认不出 model 行的格式时,推理档位控件不渲染,保存时连值一起没了

丢了不报错,但角色会悄悄退化成默认行为——比如 memory 丢了,跨项目沉淀就断开。

做法:每次会话启动,扫描角色目录,缺哪个补哪个。幂等——字段都在就一个字不动。
修复前先备份、修复后写日志,并且只在**真的修过**的时候才向会话注入一条提示。

用法(注册为 SessionStart hook):
  python3 ~/.zcode/agents/heal_frontmatter.py

手动检查(只报告不改动):
  python3 ~/.zcode/agents/heal_frontmatter.py --check

环境变量(测试用):
  ZC_AGENTS_DIR  角色目录,默认 ~/.zcode/agents
  ZC_BACKUP_DIR  备份目录,默认 ~/.zcode/frontmatter-backup
"""
import json
import os
import re
import shutil
import sys
import time

AGENTS_DIR = os.environ.get("ZC_AGENTS_DIR") or os.path.expanduser("~/.zcode/agents")
BACKUP_DIR = os.environ.get("ZC_BACKUP_DIR") or os.path.expanduser("~/.zcode/frontmatter-backup")
LOG = os.path.join(BACKUP_DIR, "heal.log")

# 角色文件 → 必须具备的字段(值固定)。没列进来的角色不动——
# 有些角色的字段"少"就是设计如此,不该被无差别补齐。
REQUIRED = {
    "first-reviewer-a.md": {"memory": "user", "thoughtLevel": "max"},
    "first-reviewer-b.md": {"memory": "user", "thoughtLevel": "max"},
    "reviewer.md": {"memory": "user", "thoughtLevel": "max"},
    "impl-worker.md": {"memory": "user", "thoughtLevel": "max"},
    "doc-writer.md": {"memory": "user"},
    "vision-reader.md": {"thoughtLevel": "max"},
}

FM_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.S)


def repair(path, fields, ts, check_only=False):
    """返回缺失字段列表;无缺失返回 None。结构异常一律不动(保守)。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()

    m = FM_RE.match(text)
    if not m:
        return None  # frontmatter 结构异常:不猜,不动

    head, fm, tail = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]

    missing, repaired = [], fm
    for key, val in fields.items():
        if not re.search(rf"^{re.escape(key)}:", repaired, re.M):
            repaired += f"\n{key}: {val}" if repaired else f"{key}: {val}"
            missing.append(f"{key}: {val}")

    if not missing or check_only:
        return missing or None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    shutil.copy2(path, os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{ts}"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + repaired + tail + rest)
    return missing


def main():
    check_only = "--check" in sys.argv[1:]
    ts = time.strftime("%Y%m%d-%H%M%S")
    fixed = []

    for name, fields in REQUIRED.items():
        path = os.path.join(AGENTS_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            missing = repair(path, fields, ts, check_only)
        except Exception as e:  # 单个角色出错不影响其他角色
            _log(f"{ts} ERROR {name}: {e}")
            continue
        if missing:
            fixed.append(f"{name}: +{', '.join(missing)}")
            if not check_only:
                _log(f"{ts} {name} 修复 {', '.join(missing)}")

    if check_only:
        if fixed:
            print("以下角色缺字段(未改动):")
            for item in fixed:
                print("  " + item)
            return 0
        print("frontmatter 完整,无需修复。")
        return 0

    if fixed:
        print(json.dumps({
            "additionalContext": (
                "[frontmatter 自愈] 面板保存丢失了字段,已自动恢复: "
                + "; ".join(fixed)
                + f"。备份与日志: {BACKUP_DIR}"
            )
        }, ensure_ascii=False))
    # 无修复则不输出——不打扰会话
    return 0


def _log(line):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 自愈脚本自身异常绝不阻塞会话
        _log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} FATAL {e}")
        sys.exit(0)
