#!/usr/bin/env python3
"""review_role_rotation.py — 第一审双模型轮换(PreToolUse hook)

目标:同一个会话(一次开发任务)内,让第一审的两个成员自动交替派发,
使同一任务天然获得**两个模型的独立审查视角**,而不是每次都落在同一个模型上。

规则:
  - 只在 tool=Agent/Task 且 subagent_type ∈ PAIR 时生效;其余派发零影响
  - SendMessage(resume)不轮换——agent 的角色/模型在创建时已固定
  - 同会话交替:上次派 B → 本次派 A;上次派 A → 本次派 B
  - 新会话的起始成员按**全局 parity** 交替(避免所有会话都从同一个成员起步,长期偏斜)
  - **默认后台**:tool_input 未显式带 run_in_background 时注入 true,
    审查在后台跑、不阻塞主会话;显式传 false 则保持前台
    (注意:某些"闲时/低峰"模式下引擎不接受后台派发,需显式传 false)
  - prompt 或 description 里含 REVIEW-ROLE-FIXED → 尊重本次指定成员(仅当次,勿写进任务书模板)
  - 改写通过 hookSpecificOutput.updatedInput(完整保留其余字段),
    并在 prompt 前加一行「本次实际角色」说明(任务书若写死角色名,以该行为准)
  - **绝不 deny**:本 hook 永不阻断派发,任何异常一律 exit 0 放行
  - 状态用 flock 保护;200 会话 / 30 天 GC

用法(在 ZCode 的 config.json 里注册为 PreToolUse hook,matcher 匹配 Agent/Task/SendMessage):
  python3 ~/.zcode/agents/review_role_rotation.py pre
"""
import sys, os, json, datetime, fcntl, contextlib

STATE = os.environ.get('RR_STATE') or os.path.expanduser('~/.zcode/agents/.review-rotation.json')
LOG = os.environ.get('RR_LOG') or os.path.expanduser('~/.zcode/agents/.review-rotation.log')
GUARD = STATE + '.guard'

# 轮换对:两个成员 = 同一审查职责的两个异构模型。改这里即可换成员。
PAIR = ('first-reviewer-b', 'first-reviewer-a')

MARK = 'REVIEW-ROLE-FIXED'   # 本次固定标记
MAX_SESSIONS = 200
MAX_AGE_DAYS = 30
LOG_MAX = 2 * 1024 * 1024


@contextlib.contextmanager
def _guard():
    fd = os.open(GUARD, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _log(event, detail='', session=''):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + '.1')
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                                'event': event, 'session': session, 'detail': detail},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass


def _load():
    try:
        d = json.load(open(STATE, encoding='utf-8'))
        if isinstance(d, dict) and isinstance(d.get('sessions'), dict):
            return d
    except Exception:
        pass
    return {'global_last_start': None, 'sessions': {}}


def _save(state):
    """绝不抛:写不进去时轮换退化为「本次按请求角色执行」,不阻断派发。"""
    try:
        tmp = STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE)
        return True
    except Exception as e:
        _log('state-save-fail', '%s: %s' % (type(e).__name__, e))
        return False


def _gc(state):
    now = datetime.datetime.now()
    sessions = state.get('sessions', {})
    for sid in list(sessions.keys()):
        try:
            ts = datetime.datetime.fromisoformat((sessions[sid] or {}).get('ts', ''))
            if (now - ts).days > MAX_AGE_DAYS:
                sessions.pop(sid, None)
        except Exception:
            sessions.pop(sid, None)
    if len(sessions) > MAX_SESSIONS:
        for sid, _ in sorted(sessions.items(), key=lambda kv: (kv[1] or {}).get('ts', ''))[:len(sessions) - MAX_SESSIONS]:
            sessions.pop(sid, None)


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode != 'pre':
        sys.exit(0)
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    tool = _pick(data, 'tool_name', 'toolName') or (data.get('tool') or {}).get('name', '')
    if tool not in ('Agent', 'Task'):
        sys.exit(0)  # SendMessage resume 不轮换:角色/模型创建时已固定

    ti = data.get('tool_input') or data.get('toolInput') or (data.get('tool') or {}).get('input') or {}
    if not isinstance(ti, dict):
        sys.exit(0)

    role = _pick(ti, 'subagent_type', 'subagentType', 'agent_type', 'agentType')
    if role not in PAIR:
        sys.exit(0)

    session = _pick(data, 'session_id', 'sessionId') or os.environ.get('CLAUDE_SESSION_ID', '')
    if not session:
        _log('skip-no-session', '载荷无 session_id,不轮换')
        sys.exit(0)

    prompt = ti.get('prompt') if isinstance(ti.get('prompt'), str) else ''
    desc = ti.get('description') if isinstance(ti.get('description'), str) else ''
    fixed = MARK in prompt or MARK in desc

    chosen, prev = role, None
    with _guard():
        state = _load()
        sessions = state.setdefault('sessions', {})
        now = datetime.datetime.now().isoformat(timespec='seconds')
        rec = sessions.get(session)
        if not isinstance(rec, dict):
            # 新会话:起始成员按全局 parity(上次全局从 B 起 → 这次从 A 起)
            last_start = state.get('global_last_start')
            start = PAIR[1] if last_start == PAIR[0] else PAIR[0]
            rec = {'last': None, 'ts': now}
            sessions[session] = rec
            state['global_last_start'] = start
            chosen = start
            _log('new-session', 'start=%s' % start, session)
        else:
            prev = rec.get('last')
            chosen = PAIR[1] if prev == PAIR[0] else PAIR[0]
        if fixed:
            chosen = role
        rec['last'] = chosen
        rec['ts'] = now
        _gc(state)
        _save(state)

    # 默认后台:未显式给 run_in_background 时注入 true —— 常规会话不阻塞主会话。
    # 显式 false = 前台,原样保留:某些低峰/闲时模式对 background 直接抛错,
    # 不可改成"配置项强制 background"(那样会让那类派发必失败)。
    inject_bg = 'run_in_background' not in ti
    rotated = chosen != role
    if not rotated and not inject_bg:
        sys.exit(0)

    new_ti = dict(ti)
    ctx = []
    if rotated:
        new_ti['subagent_type'] = chosen
        if prompt:
            new_ti['prompt'] = ('[review-rotation] 本次实际角色=%s（任务书如写死角色名，以本行为准）。\n\n'
                                % chosen) + prompt
        ctx.append('本次自动改用 %s（上次=%s）；如需固定成员，在本次派发 prompt 写 %s（仅当次，勿写入任务书模板）'
                   % (chosen, prev or '(新会话)', MARK))
    if inject_bg:
        new_ti['run_in_background'] = True
        ctx.append('本次按默认后台派发（run_in_background=true，不阻塞主会话；闲时任务请显式传 false 走前台）')

    _log('rewrite', 'role=%s->%s bg_injected=%s (prev=%s)' % (role, chosen, inject_bg, prev), session)
    try:
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'updatedInput': new_ti,
            'additionalContext': '[review-rotation] ' + '；'.join(ctx)}}, ensure_ascii=False))
    except Exception:
        pass
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        _log('fail-open', '%s: %s' % (type(e).__name__, e))
        sys.exit(0)
