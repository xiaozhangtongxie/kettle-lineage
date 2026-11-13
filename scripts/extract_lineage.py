# -*- coding: utf-8 -*-
"""Kettle 血缘抽取：作业(kjb) → 存储过程/SQL 步骤 → 表

用法：
  python extract_lineage.py --dump 存储过程导出.txt \
      --job "dms 日常调度=D:/etl/day_dms" --job "sfa 日常调度=D:/etl/day_sfa" \
      --out lineage.json

解析规则（详见 references/lineage-rules.md）：
  1. 存储过程按 SQL 语句级配对（该语句的 FROM/JOIN → 该语句的写入目标）
  2. CREATE [TEMP] TABLE ... AS 计入写入；临时表建边后做图收缩
  3. Kettle 转换的 TableOutput 沿内部 hops 反向回溯到 TableInput 取源表
"""
import argparse
import json
import os
import re
import xml.etree.ElementTree as ET


def parse_args():
    ap = argparse.ArgumentParser(description='Kettle 血缘抽取')
    ap.add_argument('--dump', help='存储过程全量导出文件（txt/sql）；不提供则只做 Kettle 侧血缘')
    ap.add_argument('--bat', action='append', default=[], metavar='BAT',
                    help='调度入口 bat 文件（推荐）：自动解析 Kitchen -file=xxx.kjb 作为链路起点，可重复')
    ap.add_argument('--job', action='append', default=[], metavar='名称=目录',
                    help='（旧用法）名称=Kettle 作业目录，自动识别目录内的主作业，可重复')
    ap.add_argument('--bat-dir', action='append', default=[], metavar='DIR',
                    help='目录：自动扫描该目录下所有同步类 bat 作为入口（自动排除 bak/copy/test），可重复')
    ap.add_argument('--schema', action='append', default=[], metavar='SCHEMA',
                    help='补充合法库名（可重复）；一般无需指定，程序会自动探测')
    ap.add_argument('--allow-bare', action='store_true',
                    help='保留不带库名的表（默认只保留已知前缀，避免 CTE/别名噪音）')
    ap.add_argument('--no-schema-filter', action='store_true',
                    help='不校验库名白名单（保留别名黑名单），用于库名未知的新项目')
    ap.add_argument('--out', required=True, help='输出 lineage.json')
    return ap.parse_args()


ARGS = parse_args()
DUMP = ARGS.dump
# ---------- 存储过程库（可选）----------
raw = ''
if DUMP:
    if not os.path.exists(DUMP):
        print('[警告] 存储过程导出不存在，改为只解析 Kettle 侧：', DUMP)
    else:
        raw = open(DUMP, 'r', encoding='utf-8', errors='replace').read()
else:
    print('[提示] 未提供 --dump：只解析 Kettle 侧血缘，CALL 到的存储过程内部读写表无法展开')
header_re = re.compile(r'(?m)^CREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION)\s+([^\s(]+)')
matches = list(header_re.finditer(raw))
procs = {}
for i, m in enumerate(matches):
    end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
    procs.setdefault(m.group(2).strip().replace('"', '').lower(), raw[m.start():end])

IDENT = r'[\w\u4e00-\u9fff]+(?:\.[\w\u4e00-\u9fff]+){0,2}'
WRITE_PAT = [re.compile(r'\bINSERT\s+INTO\s+(' + IDENT + ')', re.I),
             re.compile(r'\bUPDATE\s+(' + IDENT + r')(?!\s*\()', re.I),
             re.compile(r'\bDELETE\s+FROM\s+(' + IDENT + ')', re.I),
             re.compile(r'\bTRUNCATE\s+(?:TABLE\s+)?(' + IDENT + ')', re.I),
             re.compile(r'\bMERGE\s+INTO\s+(' + IDENT + ')', re.I),
             re.compile(r'\bCREATE\s+(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+'
                        r'(?:IF\s+NOT\s+EXISTS\s+)?(' + IDENT + ')', re.I)]
READ_PAT = [re.compile(r'\bFROM\s+(' + IDENT + ')', re.I),
            re.compile(r'\bJOIN\s+(' + IDENT + ')', re.I)]
CALL_RE = re.compile(r'\bCALL\s+((?:[\w\u4e00-\u9fff]+\.)?[\w\u4e00-\u9fff]+)\s*\(', re.I)
EXCLUDE = {'select', '(', 'dual', 'unnest', 'generate_series', 'values', 'lateral'}
NOISE_RE = re.compile(r'^(pg_|information_schema)')
# 调度日志/审计类过程：各项目命名不同，按通用约定识别（sp_*_log / sp_*_logs 结尾）。
# 这类过程几乎被每个业务过程调用，纳入血缘会淹没主线，统一丢弃。
LOG_PROC_RE = re.compile(r'(?:^|\.)sp_\w*_logs?$', re.I)


def is_log_proc(name):
    return bool(LOG_PROC_RE.search(name or ''))
# 通用数仓层名（所有项目通用的分层命名，不绑定任何业务库）
COMMON_SCHEMAS = {'ods', 'dwd', 'dws', 'dw', 'dm', 'ads', 'stg', 'stage', 'raw', 'landing',
                  'tmp', 'temp', 'dbo', 'public', 'ods_backup'}
# 常见表名前缀：裸表名（不带库名）的白名单，用于挡掉 CTE/别名噪音
# 裸表名前缀白名单（通用表命名习惯，不是库名）
BARE_OK = re.compile(r'^(idp_|fact_|dim_|fct_|v_|sys_|dms_|ods_|tmp_|temp_|t_\d|'
                     r'stg_|stage_|pm_|rb_|dwd_|dws_|ads_|raw_|mzt_|tbl_|dm_)')

# ---- 库名自动探测：从 ktr/kjb 里 SQL 表位置 + <schema> 标签推断，与项目无关 ----
_SCHEMA_HINT = re.compile(
    r'\b(?:from|join|into|update|table|truncate(?:\s+table)?)\s+'
    r'([a-z_][a-z0-9_]{1,30})\s*\.\s*([a-z_][a-z0-9_]*)', re.I)
_SCHEMA_TAG = re.compile(r'<schema>\s*([a-z_][\w]*)\s*</schema>', re.I)
# 作为"裸表名"出现（FROM xxx 后面不跟点）——用于排除"表.字段"被误判成"库.表"
_BARE_HINT = re.compile(
    r'\b(?:from|join|into|update|table|truncate(?:\s+table)?)\s+'
    r'([a-z_][a-z0-9_]{2,40})\b(?!\s*\.)', re.I)
_SCHEMA_BAD = {'dual', 'select', 'public', 'information_schema', 'pg_catalog', 'value',
               'values', 'varchar', 'number', 'date', 'timestamp', 'int', 'decimal',
               'char', 'text', 'json', 'jsonb', 'null', 'true', 'false', 'sysdate'}
# 常见字段名后缀/短名：用于区分「库.表」和「表.字段」（order_list.created_time 是后者）
_FIELD_SUFFIX = ('_time', '_date', '_id', '_name', '_code', '_flag', '_status', '_type',
                 '_desc', '_no', '_dt', '_amt', '_qty', '_num', '_count', '_key', '_value',
                 '_remark', '_note', '_remark2', '_by')
_FIELD_SHORT = {'id', 'name', 'code', 'age', 'count', 'num', 'value', 'amount', 'qty',
                'flag', 'status', 'type', 'desc', 'remark', 'note', 'dt', 'date', 'time'}


def _looks_field(n):
    """像字段名（而非表名）"""
    return n in _FIELD_SHORT or n.endswith(_FIELD_SUFFIX)


def _walk_bats(d):
    """目录下可作入口的同步类 bat（排除备份/副本/测试脚本）"""
    out = []
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        low = f.lower()
        if not low.endswith('.bat'):
            continue
        if low.startswith(('copy of', '~$')) or 'bak' in low or 'test' in low:
            continue
        out.append(os.path.join(d, f))
    return out


def detect_schemas(dirs, extra_text=''):
    """自动探测库名：SQL 表位置出现的 schema.table 前缀 + ktr 的 <schema> 标签

    过滤规则：候选库名下必须出现过 >=2 张不同的表（否则更可能是"表名.字段"写法），
    或该库名来自 ktr 的 <schema> 标签（这是 Kettle 里的显式配置，最可信）。
    extra_text 传存储过程导出正文——有些业务库只出现在过程体里。
    """
    from collections import Counter, defaultdict
    cnt = Counter()
    tabs = defaultdict(set)
    tagged = set()
    bare = set()

    def scan(text):
        low = text.lower()
        for m in _SCHEMA_HINT.finditer(low):
            prefix, tbl = m.group(1), m.group(2)
            if prefix in _SCHEMA_BAD:
                continue
            cnt[prefix] += 1
            tabs[prefix].add(tbl)
        for m in _SCHEMA_TAG.finditer(low):
            tagged.add(m.group(1))
        for m in _BARE_HINT.finditer(low):
            bare.add(m.group(1))

    if extra_text:
        scan(extra_text)
    for d in dirs:
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [x for x in subdirs if x not in ('.git', '__pycache__', 'node_modules')]
            for f in files:
                if not f.lower().endswith(('.ktr', '.kjb')):
                    continue
                try:
                    scan(open(os.path.join(root, f), encoding='utf-8', errors='ignore').read())
                except Exception:
                    pass

    out = set()
    for k, tbls in tabs.items():
        if len(k) < 2 or k in _SCHEMA_BAD:
            continue
        # 交叉验证：该名字在 SQL 里也当过裸表名 → 它是表，不是库（如 order_list.created_time）
        if k in bare and k not in tagged:
            continue
        # ① ktr 里显式写成 <schema>：最可信
        if k in tagged:
            out.add(k)
            continue
        # ② 出现频次很高
        if cnt[k] >= 10:
            out.add(k)
            continue
        # ③ 看它下面跟的名字像不像"表"：全是字段名 → 判为「表.字段」，不是库
        real_tables = [t for t in tbls if not _looks_field(t)]
        if len(tbls) >= 2:
            if real_tables and len(real_tables) * 2 >= len(tbls):
                out.add(k)
        elif len(real_tables) == 1:
            out.add(k)
    out |= {t for t in tagged if len(t) >= 2 and t not in _SCHEMA_BAD and t not in bare}
    return out


# 把 --bat-dir 展开成 --bat 列表
for _d in (ARGS.bat_dir or []):
    ARGS.bat.extend(_walk_bats(_d))

# 收集要扫描的目录（bat 所在目录 / bat-dir / job 目录）
_scan_dirs = set()
for _b in ARGS.bat:
    _bd = os.path.dirname(os.path.abspath(_b))
    if os.path.isdir(_bd):
        _scan_dirs.add(_bd)
for _d in (ARGS.bat_dir or []):
    if os.path.isdir(_d):
        _scan_dirs.add(os.path.abspath(_d))
for _spec in (ARGS.job or []):
    _jd = _spec.split('=', 1)[-1].strip()
    if os.path.isdir(_jd):
        _scan_dirs.add(os.path.abspath(_jd))

DETECTED = detect_schemas(_scan_dirs, raw)
if DETECTED:
    _list = sorted(DETECTED)
    print('[探测] 自动识别库名 %d 个: %s%s' % (len(_list), ', '.join(_list[:12]),
                                           ' …' if len(_list) > 12 else ''))
else:
    print('[提示] 未探测到库名，使用通用层名；如结果缺表可加 --schema 或 --no-schema-filter')

# 库名白名单 = 自动探测 ∪ 通用层名 ∪ --schema；--no-schema-filter 时放开（仅用别名黑名单）
SCHEMAS = DETECTED | COMMON_SCHEMAS | {x.strip().lower() for x in (ARGS.schema or [])}
if ARGS.no_schema_filter:
    SCHEMAS = None
ALIAS_BAD = {'a', 'b', 'c', 'd', 'e', 'x', 'y', 'z', 't', 's', 'tmp', 'tt'} | {f't{i}' for i in range(0, 20)}


def strip_comments(text):
    return '\n'.join(ln[:ln.find('--')] if '--' in ln else ln for ln in text.splitlines())


# 全量已出现过的“库.表”名 → 出现频次，用于把不带库名的表归一到正确的库
QUALIFIED = {}
_KNOWN_FOR_Q = SCHEMAS if SCHEMAS is not None else (DETECTED | COMMON_SCHEMAS)
for _m in re.finditer(r'\b([a-z_][\w]*)\.([a-z_][\w]*)\b', raw, re.I):
    if _m.group(1).lower() in _KNOWN_FOR_Q:
        _k = _m.group(1).lower() + '.' + _m.group(2).lower()
        QUALIFIED[_k] = QUALIFIED.get(_k, 0) + 1

# SQL 里显式声明的临时表（CREATE TEMPORARY TABLE xxx）：名字不一定带 tmp_ 前缀，
# 但确实是会话级中间表，参与血缘后要收缩掉
_TEMP_DECL_RE = re.compile(
    r'\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\s+TABLE\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?([\w."`\[\]]+)', re.I)
TEMP_DECLARED = set()
for _body in procs.values():
    for _m in _TEMP_DECL_RE.finditer(_body):
        TEMP_DECLARED.add(_m.group(1).strip('"`[]').lower().split('.')[-1])
if TEMP_DECLARED:
    print('[识别] CREATE TEMPORARY TABLE 声明的中间表 %d 个' % len(TEMP_DECLARED))


def clean_table(t):
    t = t.strip().strip('`"[]').lower()
    if not t or t in EXCLUDE or re.search(r'\w+\(', t) or '${' in t \
            or NOISE_RE.search(t) or t.startswith('log.'):
        return None
    if '.' in t:
        parts = t.split('.')
        schemas = SCHEMAS
        if len(parts) == 3:
            if schemas is None:
                return parts[1] + '.' + parts[2]
            if parts[1] in schemas:
                return parts[1] + '.' + parts[2]      # report_db.ods.x → ods.x
            if parts[0] in schemas:
                return parts[1] + '.' + parts[2]      # dm.dbo.x → dbo.x
            return None
        if len(parts) != 2:
            return None
        if schemas is None:
            if parts[0] in ALIAS_BAD:
                return None
            return t
        if parts[0] not in schemas:
            return None
        return t
    # 裸表名：按「库层级优先 + 出现频次」归一（频次能避免同名表串到错误的库）
    _prio = ['ods', 'dwd', 'dws', 'dw', 'dm', 'ads', 'stg', 'stage', 'raw', 'landing']
    _known = SCHEMAS if SCHEMAS is not None else (DETECTED | COMMON_SCHEMAS)

    def _rank(q):
        sch = q.split('.')[0]
        return (_prio.index(sch) if sch in _prio else 99, -QUALIFIED.get(q, 0), q)

    _cands = sorted((q for q in QUALIFIED if q.endswith('.' + t)), key=_rank)
    if len(_cands) == 1:
        return _cands[0]
    if _cands:
        # 多候选：频次明显占优（>=2 倍于次高）才归一，否则宁可不猜
        top_n = QUALIFIED.get(_cands[0], 0)
        rest_n = max((QUALIFIED.get(q, 0) for q in _cands[1:]), default=0)
        if top_n >= 2 * max(rest_n, 1):
            return _cands[0]
    if BARE_OK.match(t):
        return t
    if ARGS.allow_bare and len(t) >= 4 and t not in ALIAS_BAD:
        return t
    return None


def analyze_sql(sql):
    b = strip_comments(sql)
    writes = [t for p in WRITE_PAT for m in p.finditer(b) if (t := clean_table(m.group(1)))]
    reads = [t for p in READ_PAT for m in p.finditer(b) if (t := clean_table(m.group(1)))]
    calls = []
    for m in CALL_RE.finditer(b):
        c = m.group(1).strip().lower()
        c = c if '.' in c else 'ods.' + c
        if c in procs and c not in calls and not is_log_proc(c):
            calls.append(c)

    def dd(s):
        seen, out = set(), []
        for x in s:
            if x not in seen:
                seen.add(x); out.append(x)
        return out
    return dd(calls), dd(reads), dd(writes)


def split_statements(sql):
    """按分号切分 SQL 语句（忽略引号内与括号内的分号）"""
    out, buf, quote, depth = [], [], None, 0
    for ch in sql:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == '(':
            depth += 1; buf.append(ch)
        elif ch == ')':
            depth = max(0, depth - 1); buf.append(ch)
        elif ch == ';' and depth == 0:
            out.append(''.join(buf)); buf = []
        else:
            buf.append(ch)
    if ''.join(buf).strip():
        out.append(''.join(buf))
    return out


def analyze_proc(body):
    """过程体 → 调用/读/写 + 语句级配对（每条 INSERT/UPDATE 只跟它自己语句里的来源表配对）"""
    calls, reads, writes, pairs = [], [], [], []
    for st in split_statements(strip_comments(body)):
        if not st.strip():
            continue
        c, r, w = analyze_sql(st)
        for x in c:
            if x not in calls:
                calls.append(x)
        for x in r:
            if x not in reads:
                reads.append(x)
        for x in w:
            if x not in writes:
                writes.append(x)
        if w:
            pairs.append({'r': r, 'w': w})
    return calls, reads, writes, pairs


info = {}
for n, body in procs.items():
    c, r, w, prs = analyze_proc(body)
    info[n] = {'calls': c, 'reads': r, 'writes': w, 'pairs': prs}


# 数仓分层命名约定（通用，与具体项目无关）：库名前缀 → 数据层
_LAYER_RULES = (
    ('dwd', 'dw'), ('dws', 'dw'), ('dw', 'dw'), ('ods', 'ods'),
    ('dm', 'dm'), ('ads', 'dm'), ('his', 'his'), ('stg', 'ods'),
    ('stage', 'ods'), ('raw', 'ods'), ('landing', 'ods'),
)


def layer_of(t):
    """按库名前缀推断数据层；不在数仓层约定内的库都视为源系统（src）"""
    parts = t.split('.')
    if len(parts) == 1:
        return 'misc'
    sch = parts[0]
    for key, lv in _LAYER_RULES:
        if sch == key or sch.startswith(key + '_'):
            return lv
    return 'src'


# ---------- kjb/ktr ----------
def resolve_ref(ref, base_dir):
    return ref.replace('${Internal.Entry.Current.Directory}', base_dir).strip()


def parse_kjb(path):
    root = ET.parse(path).getroot()
    entries, hops = [], []
    for e in root.find('entries').findall('entry'):
        entries.append({'name': e.findtext('name') or '', 'type': e.findtext('type') or '',
                        'ref': (e.findtext('filename') or e.findtext('jobname') or '').strip(),
                        'sql': (e.findtext('sql') or '').strip()})
    hp = root.find('hops')
    if hp is not None:
        for h in hp.findall('hop'):
            hops.append((h.findtext('from'), h.findtext('to')))
    adj = {}
    for f, t in hops:
        adj.setdefault(f, []).append(t)
    names = [e['name'] for e in entries]
    order, seen = [], set()
    q = [e['name'] for e in entries if e['type'] == 'SPECIAL']
    for x in q:
        seen.add(x); order.append(x)
    while q:
        cur = q.pop(0)
        for nxt in adj.get(cur, []):
            if nxt not in seen and nxt in names:
                seen.add(nxt); order.append(nxt); q.append(nxt)
    for n in names:
        if n not in seen:
            order.append(n); seen.add(n)
    emap = {e['name']: e for e in entries}
    return [emap[n] for n in order]


def parse_ktr(path):
    root = ET.parse(path).getroot()
    steps = []
    for s in root.findall('step'):
        steps.append({'name': s.findtext('name') or '', 'type': s.findtext('type') or '',
                      'sql': (s.findtext('sql') or '').strip(),
                      'table': (s.findtext('table') or s.findtext('tablename') or '').strip(),
                      'schema': (s.findtext('schema') or '').strip()})
    names = [s['name'] for s in steps]
    adj, inc = {}, set()
    oe = root.find('order')
    if oe is not None:
        for h in oe.findall('hop'):
            f, t = h.findtext('from'), h.findtext('to')
            if f in names and t in names:
                adj.setdefault(f, []).append(t); inc.add(t)
    ordered, seen = [], set()
    q = [n for n in names if n not in inc]
    seen.update(q)
    while q:
        cur = q.pop(0)
        ordered.append(cur)
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt); q.append(nxt)
    for n in names:
        if n not in seen:
            ordered.append(n); seen.add(n)
    smap = {s['name']: s for s in steps}
    return [smap[n] for n in ordered if n in smap]


SKIP = {'SPECIAL', 'SUCCESS', 'MAIL', 'ADD_RESULT_FILENAMES'}


def collect(path, depth, sinks, chain):
    """递归收集一个 kjb 内所有 SQL/ktr 步骤 → 过程 + 内联表"""
    if depth > 6:
        return
    base = os.path.dirname(path)
    try:
        entries = parse_kjb(path)
    except Exception:
        return
    for e in entries:
        et = e['type']
        if et in SKIP:
            continue
        if et == 'SQL' and e['sql']:
            calls, reads, writes = analyze_sql(e['sql'])
            sinks['procs'].extend(calls)
            add_step(sinks, e['name'], reads, writes, e['sql'])
        elif et in ('JOB', 'WORKFLOW'):
            ref = resolve_ref(e['ref'], base)
            if ref and os.path.exists(ref) and ref.lower().endswith('.kjb'):
                collect(ref, depth + 1, sinks, chain)
        elif et in ('TRANS', 'TRANSFORMATION'):
            ref = resolve_ref(e['ref'], base)
            if not (ref and os.path.exists(ref) and ref.lower().endswith('.ktr')):
                continue
            for st in parse_ktr(ref):
                calls, reads, writes = analyze_sql(st['sql']) if st['sql'] else ([], [], [])
                sinks['procs'].extend(calls)
                tgt = qual(st)
                if st['type'] == 'TableOutput':
                    srcs = upstream_sources(ref, st['name'])
                    add_step(sinks, st['name'], srcs, [tgt] if tgt else [], st['sql'])
                else:
                    w = list(writes)
                    if tgt:
                        w.append(tgt)
                    add_step(sinks, st['name'], reads, w, st['sql'])


_ktr_cache = {}


def ktr_graph(path):
    """解析转换的步骤与连线，供回溯 TableOutput 的源头"""
    if path in _ktr_cache:
        return _ktr_cache[path]
    try:
        root = ET.parse(path).getroot()
    except Exception:
        _ktr_cache[path] = ({}, {})
        return _ktr_cache[path]
    steps = {}
    for st in root.findall('step'):
        steps[st.findtext('name') or ''] = {
            'type': st.findtext('type') or '',
            'table': (st.findtext('table') or '').strip(),
            'schema': (st.findtext('schema') or '').strip(),
            'sql': (st.findtext('sql') or '').strip(),
        }
    rev = {}
    oe = root.find('order')
    if oe is not None:
        for h in oe.findall('hop'):
            f, t = h.findtext('from'), h.findtext('to')
            if f in steps and t in steps:
                rev.setdefault(t, []).append(f)
    _ktr_cache[path] = (steps, rev)
    return _ktr_cache[path]


def qual(st):
    if not st.get('table') or '${' in st['table']:
        return None
    full = (st['schema'].lower() + '.' if st['schema'] else '') + st['table'].lower()
    return clean_table(full)


def upstream_sources(path, step_name, max_depth=6):
    """沿转换内部连线向上找数据来源：TableInput 的表 / SQL 里的读取表"""
    steps, rev = ktr_graph(path)
    seen, frontier, depth = set(), [step_name], 0
    srcs = []
    while frontier and depth < max_depth:
        nxt = []
        for n in frontier:
            for prev in rev.get(n, []):
                if prev in seen:
                    continue
                seen.add(prev)
                nxt.append(prev)
        frontier = nxt
        depth += 1
    for n in seen:
        st = steps.get(n, {})
        t = qual(st)
        if t:
            srcs.append(t)
        elif st.get('sql'):
            srcs.extend(analyze_sql(st['sql'])[1])
    return list(dict.fromkeys(srcs))


def is_tmp(t):
    """临时表/中间表：图中不展示，但在血缘里作为中转保留，最后收缩掉

    三类：tmp_/temp_ 前缀、t_数字、SQL 里 CREATE TEMPORARY TABLE 显式声明的名字。
    """
    last = t.split('.')[-1]
    if last in TEMP_DECLARED:
        return True
    return last.startswith(('tmp_', 'temp_')) or bool(re.match(r'^t_\d+$', last))


def all_calls(sql):
    """SQL 里出现的全部 CALL 过程名（不论是否有定义）"""
    names = []
    for m in CALL_RE.finditer(strip_comments(sql)):
        c = m.group(1).strip().lower()
        c = c if '.' in c else 'ods.' + c
        if c not in names:
            names.append(c)
    return names


def add_step(sinks, label, reads, writes, sql=''):
    """按 Kettle 步骤记录内联 SQL 的读写表（步骤级，不跨作业合并）"""
    writes = list(dict.fromkeys(writes))
    reads = [t for t in dict.fromkeys(reads) if t not in writes]
    unresolved = [c for c in all_calls(sql) if c not in procs and not is_log_proc(c)] if sql else []
    if writes or reads or unresolved:
        sinks['steps'].append({'label': label, 'r': reads, 'w': writes, 'unresolved': unresolved})


COLORS = ['#1565c0', '#00897b', '#e65100', '#6a1b9a', '#2e7d32', '#ad1457',
          '#0277bd', '#ef6c00', '#5e35b1', '#00695c', '#c62828', '#37474f']


# ---------- BAT 解析：找出它启动的 kjb / ktr ----------
def parse_bat(path):
    """从 bat 里提取 Kitchen/Pan 的 -file 参数与 -param/-p 变量"""
    calls = []
    try:
        txt = open(path, 'r', encoding='utf-8', errors='ignore').read()
    except Exception:
        return calls
    for line in txt.splitlines():
        low = line.strip().lower()
        if not low or low.startswith(('rem ', '::', '@rem')):
            continue
        if 'kitchen' not in low and 'pan' not in low and '-file' not in low:
            continue
        m = re.search(r'-file\s*=\s*"?([^"\r\n]+?\.(?:kjb|ktr))"?(?!\S)', line, re.I)
        if not m:
            continue
        params = {}
        for k, v in re.findall(r'-param\s*:?\s*([\w.\-]+)\s*=\s*"?([^"\s]+)"?', line, re.I):
            params[k] = v
        calls.append({'file': m.group(1).strip(), 'params': params, 'line': line.strip()[:200]})
    return calls


def _walk_find(root, basename, max_depth=4):
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for cur, dirs, files in os.walk(root):
        if cur.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
            continue
        if basename in files:
            return os.path.join(cur, basename)
    return None


def resolve_asset(bat_path, raw):
    """bat 里写的是生产机绝对路径（如 F:\\...），本地按 basename 就近查找"""
    raw = raw.replace('\\', '/').strip().strip('"')
    if os.path.exists(raw):
        return raw
    base = os.path.basename(raw)
    bat_dir = os.path.dirname(os.path.abspath(bat_path))
    for up in (bat_dir, os.path.dirname(bat_dir), os.path.dirname(os.path.dirname(bat_dir))):
        if not up or not os.path.isdir(up):
            continue
        cand = os.path.join(up, base)
        if os.path.exists(cand):
            return cand
        found = _walk_find(up, base)
        if found:
            return found
    return None


# ---------- 主作业自动识别 ----------
def detect_main_kjb(d):
    """识别主作业：kjb 的 entry 数最多的优先，sync_/job_/etl_ 前缀加权"""
    cands = []
    try:
        for f in sorted(os.listdir(d)):
            low = f.lower()
            if not low.endswith('.bat') or any(k in low for k in ('copy', 'bak', 'test')):
                continue
            try:
                txt = open(os.path.join(d, f), 'r', encoding='utf-8', errors='ignore').read()
            except Exception:
                continue
            m = re.search(r'([\w\-.]+\.kjb)', txt, re.I)
            if not m:
                continue
            path = os.path.join(d, m.group(1))
            if not os.path.exists(path):
                continue
            try:
                n = len(parse_kjb(path))
            except Exception:
                n = 0
            score = n * 10
            if low.startswith('sync_'):
                score += 100
            elif low.startswith(('job_', 'etl_', 'run_', 'sched')):
                score += 50
            cands.append((score, path, f))
    except Exception:
        pass
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1], cands[0][2]
    best = None
    try:
        for f in os.listdir(d):
            if f.lower().endswith('.kjb'):
                try:
                    n = len(parse_kjb(os.path.join(d, f)))
                except Exception:
                    continue
                if not best or n > best[1]:
                    best = (os.path.join(d, f), n)
    except Exception:
        pass
    return (best[0] if best else None), ''


COLORS = ['#1565c0', '#00897b', '#e65100', '#6a1b9a', '#2e7d32', '#ad1457',
          '#0277bd', '#ef6c00', '#5e35b1', '#00695c', '#c62828', '#37474f']

DATA = {'chains': []}

# 收集所有 (链路显示名, 主作业路径, bat文件名) 任务
TASKS = []
for bat in ARGS.bat:
    bat_abs = os.path.abspath(bat)
    if not os.path.exists(bat_abs):
        print('[跳过] bat 不存在:', bat)
        continue
    calls = parse_bat(bat_abs)
    if not calls:
        print('[跳过] bat 里没找到 Kitchen -file 参数:', bat_abs)
        continue
    stem = os.path.splitext(os.path.basename(bat_abs))[0]
    multi = len(calls) > 1
    for ci, call in enumerate(calls):
        target = resolve_asset(bat_abs, call['file'])
        if not target:
            print('[跳过] bat 引用的文件未找到:', call['file'])
            continue
        if not target.lower().endswith('.kjb'):
            print('[跳过] 入口不是作业文件(kjb):', target)
            continue
        label = stem if not multi else f'{stem}#{ci + 1}'
        TASKS.append((label, target, os.path.basename(bat_abs), call.get('params') or {}))

for spec in ARGS.job:
    label, _, job_dir = spec.partition('=')
    job_dir = (job_dir or label).strip()
    if not os.path.isdir(job_dir):
        print('[跳过] 目录不存在:', job_dir)
        continue
    main_kjb, bat = detect_main_kjb(job_dir)
    if not main_kjb:
        print('[跳过] 未找到主作业 kjb:', job_dir)
        continue
    TASKS.append((label.strip(), main_kjb, bat, {}))

for label, main_kjb, bat, _params in TASKS:
    cname = label
    chain = {'name': cname, 'bat': bat, 'kjb': main_kjb, 'jobs': []}
    base = os.path.dirname(main_kjb)
    for idx, e in enumerate([x for x in parse_kjb(main_kjb)
                             if x['type'] not in SKIP and (x['type'] in ('JOB', 'TRANS', 'SQL'))]):
        sinks = {'procs': [], 'steps': []}
        et, ename = e['type'], e['name']
        if et == 'SQL' and e['sql']:
            calls, reads, writes = analyze_sql(e['sql'])
            sinks['procs'].extend(calls)
            add_step(sinks, ename, reads, writes)
        elif et in ('JOB', 'WORKFLOW'):
            ref = resolve_ref(e['ref'], base)
            if ref and os.path.exists(ref) and ref.lower().endswith('.kjb'):
                collect(ref, 0, sinks, chain)
        elif et in ('TRANS', 'TRANSFORMATION'):
            ref = resolve_ref(e['ref'], base)
            if ref and os.path.exists(ref) and ref.lower().endswith('.ktr'):
                for st in parse_ktr(ref):
                    calls, reads, writes = analyze_sql(st['sql']) if st['sql'] else ([], [], [])
                    sinks['procs'].extend(calls)
                    tgt = qual(st)
                    if st['type'] == 'TableOutput':
                        srcs = upstream_sources(ref, st['name'])
                        add_step(sinks, st['name'], srcs, [tgt] if tgt else [], st['sql'])
                    else:
                        w = list(writes)
                        if tgt:
                            w.append(tgt)
                        add_step(sinks, st['name'], reads, w, st['sql'])
        seen, plist = set(), []
        for p in sinks['procs']:
            if p not in seen:
                seen.add(p)
                plist.append(p)
        procs_out = []
        for p in plist:
            pd = info.get(p, {'writes': [], 'reads': [], 'pairs': []})
            w, rd = pd.get('writes', []), pd.get('reads', [])
            pairs = pd.get('pairs', [])
            entries = [{'n': t, 'l': layer_of(t), 'd': 'wr' if t in rd else 'w'} for t in w]
            entries += [{'n': t, 'l': layer_of(t), 'd': 'r'} for t in rd if t not in w]
            entries = [x for x in entries if not is_tmp(x['n'])]
            procs_out.append({'n': p,
                              'w': len([x for x in entries if 'w' in x['d']]),
                              'r': len([x for x in entries if x['d'] == 'r']),
                              't': sorted(entries, key=lambda x: x['n']),
                              'pairs': [[st['r'], st['w']] for st in pairs]})
        for si, st in enumerate(sinks['steps']):
            entries = [{'n': t, 'l': layer_of(t), 'd': 'wr' if t in st['r'] else 'w'} for t in st['w']]
            entries += [{'n': t, 'l': layer_of(t), 'd': 'r'} for t in st['r'] if t not in st['w']]
            entries = [x for x in entries if not is_tmp(x['n'])]
            if not entries and not st['r'] and not st['w']:
                continue
            # 记录本步骤里调用了、但导出文件中没有定义的过程
            for cn in (st.get('unresolved') or []):
                pn = '（未解析过程）' + cn + '()'
                if not any(x['n'] == pn for x in procs_out):
                    procs_out.append({'n': pn, 'w': 0, 'r': 0, 't': [], 'pairs': []})
            nm = '（SQL·' + ename + '｜' + st['label'] + '）'
            if any(x['n'] == nm for x in procs_out):
                nm = '（SQL·' + ename + '｜' + st['label'] + '#' + str(si + 1) + '）'
            procs_out.append({'n': nm,
                              'w': len([x for x in entries if 'w' in x['d']]),
                              'r': len([x for x in entries if x['d'] == 'r']),
                              't': sorted(entries, key=lambda x: x['n']),
                              'pairs': [[st['r'], st['w']]]})
        chain['jobs'].append({'id': f'{len(DATA["chains"])}-{idx}', 'name': ename, 'kind': et, 'seq': idx + 1,
                              'color': COLORS[idx % len(COLORS)], 'procs': procs_out})
    DATA['chains'].append(chain)

# ---------- 表级血缘：pairs（含临时表）→ 收缩临时表 → 表级边 ----------
raw_edges = set()
for ch in DATA['chains']:
    for job in ch['jobs']:
        for node in job['procs']:
            for rs, ws in node.get('pairs', []):
                for src in rs:
                    for dst in ws:
                        if src != dst:
                            raw_edges.add((src, dst, node['n']))

edges_set = set(raw_edges)
while True:
    tmp = next((t for t in {x for e in edges_set for x in (e[0], e[1]) if is_tmp(x)}
                if any(e[1] == t for e in edges_set) and any(e[0] == t for e in edges_set)), None)
    if not tmp:
        break
    ins = [(e[0], e[2]) for e in edges_set if e[1] == tmp]
    outs = [(e[1], e[2]) for e in edges_set if e[0] == tmp]
    for a, _p1 in ins:
        for b, p2 in outs:
            if a != b:
                edges_set.add((a, b, p2))
    edges_set = {e for e in edges_set if e[0] != tmp and e[1] != tmp}
edges_set = {e for e in edges_set if not is_tmp(e[0]) and not is_tmp(e[1])}

tab_names = set()
for ch in DATA['chains']:
    for job in ch['jobs']:
        for node in job['procs']:
            for e in node['t']:
                tab_names.add(e['n'])
for e in edges_set:
    tab_names.add(e[0])
    tab_names.add(e[1])
tab_names = {t for t in tab_names if not is_tmp(t)}

by_last = {}
for t in tab_names:
    if '.' in t:
        by_last.setdefault(t.split('.')[-1], []).append(t)
alias = {}
for t in tab_names:
    if '.' not in t:
        cand = by_last.get(t, [])
        if len(cand) == 1:
            alias[t] = cand[0]


def fixname(t):
    return alias.get(t, t)


tab_names = sorted({fixname(t) for t in tab_names})
tidx = {n: i for i, n in enumerate(tab_names)}

all_tables = {n: {'l': layer_of(n), 'w': [], 'r': []} for n in tab_names}
for ch in DATA['chains']:
    for job in ch['jobs']:
        for node in job['procs']:
            for e in node['t']:
                nm = fixname(e['n'])
                if nm not in all_tables:
                    continue
                lst = all_tables[nm]['w'] if 'w' in e['d'] else all_tables[nm]['r']
                if node['n'] not in lst:
                    lst.append(node['n'])

edges_f = set()
for s0, d0, p in edges_set:
    a, b = fixname(s0), fixname(d0)
    if a != b and a in tidx and b in tidx:
        edges_f.add((tidx[a], tidx[b], p))

DATA['tables'] = [{'n': n, 'l': all_tables[n]['l'], 'w': all_tables[n]['w'], 'r': all_tables[n]['r']}
                  for n in tab_names]
DATA['edges'] = sorted([a, b, p] for a, b, p in edges_f)

proc_jobs = {}
for ch in DATA['chains']:
    for job in ch['jobs']:
        for p in job['procs']:
            lst = proc_jobs.setdefault(p['n'], [])
            key = [ch['name'], job['name'], job['seq']]
            if key not in lst:
                lst.append(key)
DATA['procJobs'] = proc_jobs

for ch in DATA['chains']:
    for job in ch['jobs']:
        for node in job['procs']:
            node.pop('pairs', None)

njobs = sum(len(c['jobs']) for c in DATA['chains'])
nproc = len({p['n'] for c in DATA['chains'] for j in c['jobs'] for p in j['procs']})
print(f'链路 {len(DATA["chains"])} 条 | 作业 {njobs} 个 | 节点 {nproc} 个 | '
      f'表 {len(DATA["tables"])} 张 | 表级血缘 {len(DATA["edges"])} 条')

_outdir = os.path.dirname(os.path.abspath(ARGS.out))
if _outdir:
    os.makedirs(_outdir, exist_ok=True)
with open(ARGS.out, 'w', encoding='utf-8') as f:
    json.dump(DATA, f, ensure_ascii=False)
print('输出:', ARGS.out)
