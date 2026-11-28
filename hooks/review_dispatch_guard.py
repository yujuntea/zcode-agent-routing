#!/usr/bin/env python3
"""review_dispatch_guard.py — 审查派发进场闸门(PreToolUse hook)

治「下一跳改写」:如果审查流程靠"上一环说好下一环是谁"来串,
而派发端可以自由发挥(不写依据、随手换个环节),那这条链随时能被悄悄跳步或改道。

做法:派第一审/终审角色时,派发内容必须带**进场依据**——要么是上一份报告的
状态行,要么是「首审」声明(链中第一环没有上一环,如实声明)。
没有依据 = 拒绝这次派发,并在 stderr 给出可照抄的补法。

依据可以写在两处,任一即可,但两处口径不同:
  1) prompt / description 文本内联一行 —— 只认两种:
       - 含 〔下一跳 的行(按「出现」判定);
       - 行首「首审」。
  2) prompt 里只给了任务书路径 → 本 hook 读该任务书,找行首的
     「上一份报告状态行原文」标题,紧随其后的声明行**以**下列之一**开头**即算:
     首审 / 状态：/ 〔下一跳 / 〔状态 / 〔本轮状态。
     (路径须绝对或 ~ 开头、不含空格,最多读 8 个文件)

  两处差别在**标记种类**,不是判定位置更严:内联 prompt 没有标题锚,
  〔下一跳 只能按「出现」判定,于是把控制不住的标记(〔状态/〔本轮状态/状态：)
  排除在外——否则一句「状态：已审」的说明就能放行。代价是明知的:
  **内联里写「状态行格式是〔下一跳:X〕」这种说明句,本 hook 也会放行**。
  这与「只校验存在性、不校验语义」是同一条契约。

已验证的判定口径:
  - 裸词「状态行」「下一跳」单独出现 → **不放行**(必须出现 〔下一跳 这个完整标记,或行首是「首审」)
  - 模板占位 `<…>` → **不放行**
  - 内联里出现完整 〔下一跳 即放行(含说明句,见上「代价是明知的」)
  - 标题行首可有 #/** 前缀;成对闭合的 fenced code block(``` 或 ~~~)内会被跳过

设计约束(勿破坏):
  - fail-open:载荷残缺或未预期异常一律 exit 0 放行——闸门缺席不该拦住流程
  - 只管审查角色(ROLES),其余派发零影响
  - SendMessage(resume)只拦在册的审查角色:登记表(.review-dispatch.agents.json)
    由 post 模式登记新派发;未登记的(存量 agent / 跨会话冷启动)放行——
    登记表是**识别器**,不是白名单
  - 阻断用 exit 2 + stderr(引擎会把 stderr 显示给模型);stdout 的结构化
    decision 字段在本引擎不合 schema,会被丢弃

用法(注册为 PreToolUse + PostToolUse + PostToolUseFailure hook):
  python3 ~/.zcode/agents/review_dispatch_guard.py pre    # 进场依据校验,缺=拒绝
  python3 ~/.zcode/agents/review_dispatch_guard.py post   # 登记 agentId→角色
"""
import sys, os, json, datetime, re, fcntl, contextlib

REG = os.environ.get('REVIEW_DISPATCH_REG') or os.path.expanduser('~/.zcode/agents/.review-dispatch.agents.json')
LOG = os.environ.get('REVIEW_DISPATCH_LOG') or os.path.expanduser('~/.zcode/agents/.review-dispatch.log')
GUARD = REG + '.guard'

# 受闸门约束的角色:只管审查家族
ROLES = ('reviewer', 'first-reviewer-a', 'first-reviewer-b')
# 需要登记(供下游反查角色)的全部角色:审查三角色 + 执行三角色
REG_ROLES = ROLES + ('impl-worker', 'doc-writer', 'vision-reader')
REG_MAX = 1000

_TASKBOOK_RE = re.compile(r'(?:~|/)[^\s"\'`()<>，。；、：]+?\.md')
# 标题可带引用块(>)、#/**、列表前缀——真实任务书里有 84 份用 `> ## 上一份…` 形态
_TASKBOOK_HEADING_RE = re.compile(r'^[ \t]*(?:>[ \t]*)*(?:#{1,4}[ \t]*)?\**[ \t]*上一份报告状态行原文'
                                  r'(?:[（(][^）)]{0,12}[）)])?')
_TASKBOOK_MAX_FILES = 8
_TASKBOOK_MAX_BYTES = 400 * 1024

_AGENT_ID_RE = re.compile(r'agentId:\s*(agent_[A-Za-z0-9-]+)')


@contextlib.contextmanager
def _guard():
    fd = os.open(GUARD, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _rotate(path, max_bytes):
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + '.1')
    except Exception:
        pass


def _log(event, detail=''):
    _rotate(LOG, 4 * 1024 * 1024)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                                'event': event, 'detail': detail}, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _deny(reason):
    _log('deny', reason)
    sys.stderr.write(reason + '\n')
    sys.exit(2)


def _reg_load():
    try:
        d = json.loads(open(REG, encoding='utf-8').read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _reg_save(reg):
    _rotate(REG, 1024 * 1024)
    try:
        with open(REG, 'w', encoding='utf-8') as f:
            json.dump(reg, f, ensure_ascii=False)
    except Exception:
        pass


def _strip_prefix(s):
    """去掉列表/引用/标题/标点前缀,便于判定「以…开头」。"""
    return re.sub(r'^(?:[-*>#\s：:，,（(【\[「『`"\'*])+', '', s.strip())


def _prompt_has_basis(*texts):
    """文本级依据:须是状态行本身或首审声明本身,不接受裸词。"""
    for t in texts:
        if not isinstance(t, str):
            continue
        if '〔下一跳' in t:
            return True
        for line in t.splitlines():
            if _strip_prefix(line).startswith('首审'):
                return True
    return False


def _taskbook_paths(*texts):
    out, seen = [], set()
    for t in texts:
        if not isinstance(t, str):
            continue
        for m in _TASKBOOK_RE.finditer(t):
            p = os.path.expanduser(m.group(0))
            if p not in seen:
                seen.add(p)
                out.append(p)
    # 任务书名优先,其余保序
    out.sort(key=lambda p: 0 if re.search(r'taskbook|任务书', os.path.basename(p), re.I) else 1)
    return out[:_TASKBOOK_MAX_FILES]


# 允许「reviewer：〔下一跳…」「glm：首审…」这类带角色标签的写法;
# 只认 ASCII 标签——中文散文(如「这里写一个检查清单：」)不算,避免模板句蒙混。
_LABEL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.\-]{0,15}\s*[：:]\s*')


def _declaration_line(s):
    """任务书里的声明行:去掉列表/引用/标题/反引号前缀后,必须以声明标记**开头**。

    只"包含"标记不算——否则一行「状态行格式是〔下一跳:X〕」的示例句就能骗过闸门。
    认的合法开头(按真实任务书统计):首审 / 状态：/ 〔下一跳 / 〔状态 / 〔本轮状态。
    """
    s = _LABEL_RE.sub('', _strip_prefix(s))
    return re.match(r'^(?:首审|状态[：:]|〔(?:下一跳|状态|本轮状态))', s) is not None


def _taskbook_has_basis(path):
    """结构判定而非词面匹配:标题锚行首(可带 #/**),跳过 fenced block,
    取其后同行剩余或首个非空行,须以「首审」或 〔下一跳 **开头**。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 2 * 1024 * 1024:
            return False
        with open(path, encoding='utf-8', errors='ignore') as f:
            lines = f.read(_TASKBOOK_MAX_BYTES).splitlines()
    except Exception:
        return False
    # 只跳过**成对闭合**的代码块:未闭合的开头按普通文本处理,
    # 否则任务书里漏写一个收尾 ``` 就会让后面所有声明行都看不见。
    fenced = set()
    open_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(('```', '~~~')):
            if open_idx is None:
                open_idx = i
            else:
                fenced.update(range(open_idx, i + 1))
                open_idx = None
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        m = _TASKBOOK_HEADING_RE.match(line)
        if not m:
            continue
        rest = line[m.end():]
        if _declaration_line(rest):
            return True
        for nxt in lines[i + 1:i + 6]:
            if not nxt.strip():
                continue
            if _declaration_line(nxt):
                return True
            break
    return False


def _basis_ok(*texts):
    if _prompt_has_basis(*texts):
        return True
    return any(_taskbook_has_basis(p) for p in _taskbook_paths(*texts))


def _extract_agent_id(resp):
    try:
        if isinstance(resp, dict):
            v = resp.get('agentId') or resp.get('agent_id')
            if isinstance(v, str) and v.startswith('agent_'):
                return v
            if isinstance(resp.get('text'), str):
                m = _AGENT_ID_RE.search(resp['text'])
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    tool = data.get('tool_name') or (data.get('tool') or {}).get('name', '')
    ti = data.get('tool_input') or data.get('toolInput') or (data.get('tool') or {}).get('input') or {}
    if not isinstance(ti, dict):
        sys.exit(0)
    if tool not in ('Agent', 'Task', 'SendMessage'):
        sys.exit(0)
    session = data.get('session_id') or os.environ.get('CLAUDE_SESSION_ID') or ''

    if mode == 'pre':
        if tool in ('Agent', 'Task'):
            role = ti.get('subagent_type')
            if role not in ROLES:
                sys.exit(0)
            if _basis_ok(ti.get('prompt'), ti.get('description')):
                sys.exit(0)
            _deny('[REVIEW-DISPATCH] 派 %s 缺进场依据:请在派发 prompt(或 description)内联一行'
                   '「上一份报告状态行原文:〔下一跳…〕」或「首审(链中第一环,无上一状态行)」'
                   '——内联路径只认这两种;prompt 只给任务书路径时,任务书内行首的'
                   '「上一份报告状态行原文」标题后以 首审 / 状态：/ 〔下一跳 / 〔状态 / 〔本轮状态'
                   '开头也认(路径须绝对/~ 开头、不含空格、最多 8 个)。'
                   '缺依据的派发可能跳步/下一跳改写。'
                   % role)
        # SendMessage: 仅登记在册的审查角色受闸门
        with _guard():
            ent = _reg_load().get(ti.get('to'))
        if ent and ent.get('role') in ROLES:
            if not _basis_ok(ti.get('message'), ti.get('summary')):
                _deny('[REVIEW-DISPATCH] resume %s(登记角色=%s)的消息文本缺进场依据:请在消息中引用'
                      '上一份报告状态行原文(〔下一跳…)或「首审」声明——消息路径只认这两种;'
                      '消息只给任务书路径时,任务书行首「上一份报告状态行原文」标题后的'
                      '首审 / 状态：/〔下一跳 /〔状态 /〔本轮状态 行也认。补依据后重发。'
                      % (ti.get('to'), ent.get('role')))
        sys.exit(0)

    if mode == 'post':
        agent_id = _extract_agent_id(data.get('toolResponse', data.get('tool_response')))
        if tool in ('Agent', 'Task') and agent_id and ti.get('subagent_type') in REG_ROLES:
            with _guard():
                reg = _reg_load()
                reg[agent_id] = {'role': ti.get('subagent_type'), 'session': session,
                                 'ts': datetime.datetime.now().isoformat(timespec='seconds')}
                if len(reg) > REG_MAX:
                    for k in sorted(reg, key=lambda a: reg[a].get('ts', ''))[:len(reg) - REG_MAX]:
                        reg.pop(k, None)
                _reg_save(reg)
            _log('register', '%s→%s' % (agent_id, ti.get('subagent_type')))
        sys.exit(0)

    sys.exit(0)  # 未知模式 fail-open


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        _log('fail-open', '%s: %s' % (type(e).__name__, e))
        sys.exit(0)
