# -*- coding: utf-8 -*-
"""对血缘结果做自检：结构自洽性 + 源码可回溯 + 覆盖率 + 金标准比对

用法：
  # 1) 已有 lineage.json
  python selftest.py --lineage output/lineage.json --dump 过程导出.txt

  # 2) 直接给 bat 与 dump（会先跑一次抽取）
  python selftest.py --dump 过程导出.txt --bat a.bat --bat b.bat

  # 3) 带金标准（可选）：expect.json = {"表名": ["必须出现的上游表", ...]}
  python selftest.py --lineage output/lineage.json --dump 过程导出.txt --expect expect.json

退出码：0 全部通过；1 存在 FAIL
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_dump(path):
    raw = open(path, 'r', encoding='utf-8', errors='replace').read()
    hdr = re.compile(r'(?m)^CREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION)\s+([^\s(]+)')
    ms = list(hdr.finditer(raw))
    procs = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(raw)
        procs.setdefault(m.group(2).strip().replace('"', '').lower(), raw[m.start():end])
    return raw, procs


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=''):
        self.rows.append((name, ok, detail))
        flag = 'PASS' if ok else ('SKIP' if ok is None else 'FAIL')
        print(f'[{flag}] {name}' + (f' — {detail}' if detail else ''))

    def done(self):
        n_fail = sum(1 for _, ok, _ in self.rows if ok is False)
        n_skip = sum(1 for _, ok, _ in self.rows if ok is None)
        n_pass = len(self.rows) - n_fail - n_skip
        print(f'\n结果：{n_pass} 通过 · {n_skip} 跳过 · {n_fail} 失败')
        return 0 if n_fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description='Kettle 血缘自检')
    ap.add_argument('--lineage', help='已有 lineage.json；不给则用 --bat 现场抽取')
    ap.add_argument('--dump', help='存储过程导出文件；不给则跳过过程相关检查')
    ap.add_argument('--bat', action='append', default=[], help='调度 bat（可重复）')
    ap.add_argument('--expect', help='金标准 JSON：{"表名": ["上游表", ...]}')
    ap.add_argument('--sample', type=int, default=40, help='抽样过程数（默认 40）')
    args = ap.parse_args()

    rp = Report()
    lineage_path = args.lineage

    # ---------- 0) 准备数据 ----------
    if not lineage_path:
        if not args.bat:
            print('需要 --lineage 或 --bat')
            return 2
        lineage_path = os.path.join(os.path.dirname(os.path.abspath(args.dump)), '_selftest_lineage.json')
        cmd = [sys.executable, os.path.join(HERE, 'extract_lineage.py'), '--dump', args.dump,
               '--out', lineage_path]
        for b in args.bat:
            cmd += ['--bat', b]
        print('运行抽取:', ' '.join(cmd[1:]))
        subprocess.run(cmd, check=True)
    D = json.load(open(lineage_path, encoding='utf-8'))
    T, E = D['tables'], D['edges']
    if args.dump and os.path.exists(args.dump):
        raw, procs = load_dump(args.dump)
    else:
        raw, procs = '', {}
        print('[提示] 未提供 --dump：跳过存储过程相关检查')
    print(f'\n数据：链 {len(D["chains"])} · 作业 {sum(len(c["jobs"]) for c in D["chains"])} · '
          f'表 {len(T)} · 边 {len(E)} · 过程定义 {len(procs)}\n')

    # ---------- 1) 结构自洽 ----------
    bad_idx = [1 for s, d, p in E if not (0 <= s < len(T) and 0 <= d < len(T))]
    self_loop = [1 for s, d, p in E if s == d]
    dup = len(E) - len({(s, d, p) for s, d, p in E})
    rp.add('边索引不越界', not bad_idx, f'越界 {len(bad_idx)} 条')
    rp.add('无自环边', not self_loop, f'自环 {len(self_loop)} 条')
    rp.add('无重复边', dup == 0, f'重复 {dup} 条')
    empty = [t['n'] for t in T if not t['w'] and not t['r']]
    rp.add('无孤立表（既无人写也无人读）', not empty, f'孤立 {len(empty)} 张' + (f'，如 {empty[:3]}' if empty else ''))

    # ---------- 2) 表名可回溯（防幻觉）----------
    # 语料 = 存储过程导出 + 所有 kjb/ktr 文本（表名也可能来自 Kettle 步骤配置）
    corpus = [raw.lower()] if raw else []
    for ch in D['chains']:
        kjb = ch.get('kjb')
        if not kjb or not os.path.exists(kjb):
            continue
        for root_dir, _, files in os.walk(os.path.dirname(os.path.abspath(kjb))):
            for f in files:
                if f.lower().endswith(('.kjb', '.ktr')):
                    try:
                        corpus.append(open(os.path.join(root_dir, f), 'r',
                                           encoding='utf-8', errors='ignore').read().lower())
                    except Exception:
                        pass
    corpus_txt = '\n'.join(corpus)
    halluc = []
    for t in T:
        n = t['n'].lower()
        if n not in corpus_txt and n.split('.')[-1] not in corpus_txt:
            halluc.append(n)
    rp.add('所有表名都能在源文件中找到', not halluc,
           f'找不到 {len(halluc)} 张' + (f'，如 {halluc[:3]}' if halluc else ''))

    # ---------- 3) 过程读写可回溯（抽样：过程里的表必须出现在过程体）----------
    real_procs = [p for ch in D['chains'] for j in ch['jobs'] for p in j['procs']
                  if not p['n'].startswith(('（SQL·', '（未解析过程）'))] if procs else []
    by_name = {p['n']: p for p in real_procs}
    names = sorted(by_name)
    random.seed(42)
    sample = random.sample(names, min(args.sample, len(names))) if names else []
    mism, checked = [], 0
    for n in sample:
        body = procs.get(n)
        if not body:
            continue
        low = body.lower()
        checked += 1
        for e in by_name[n]['t']:
            tb = e['n'].lower()
            if tb not in low and tb.split('.')[-1] not in low:
                mism.append((n, e['n']))
    if procs:
        rp.add('抽样过程中读写的表都能在过程体找到', not mism,
               f'检查 {checked} 个过程，异常 {len(mism)} 处' + (f'，如 {mism[:2]}' if mism else ''))
    else:
        rp.add('抽样过程中读写的表都能在过程体找到', None, '无存储过程导出，跳过')

    # ---------- 4) 被调用的过程是否都有定义 ----------
    if procs:
        used = sorted({n for n in names})
        missing = [n for n in used if n not in procs]
        rp.add('链路中引用的过程在导出文件中都有定义', not missing,
               f'缺失 {len(missing)} 个' + (f'，如 {missing[:3]}' if missing else ''))
    else:
        rp.add('链路中引用的过程在导出文件中都有定义', None, '无存储过程导出，跳过')

    # ---------- 5) 链路覆盖率（与主作业入口数对比）----------
    cov_rows, cov_ok = [], True
    for ch in D['chains']:
        kjb = ch.get('kjb')
        if not kjb or not os.path.exists(kjb):
            cov_rows.append(f'{ch["name"]}: 无 kjb 路径，跳过')
            continue
        root = __import__('xml.etree.ElementTree', fromlist=['ET']).parse(kjb).getroot()
        skip = {'SPECIAL', 'SUCCESS', 'MAIL', 'ADD_RESULT_FILENAMES'}
        want = [e for e in root.find('entries').findall('entry')
                if (e.findtext('type') or '') not in skip
                and (e.findtext('type') or '') in ('JOB', 'TRANS', 'SQL')]
        got = len(ch['jobs'])
        ok = len(want) == got
        cov_ok = cov_ok and ok
        cov_rows.append(f'{ch["name"]}: 期望 {len(want)} 实际 {got}')
    rp.add('每个作业入口都进了链路', cov_ok, '；'.join(cov_rows) or '无 kjb 路径')

    # ---------- 6) 表 → 过程 反向一致 ----------
    edge_bad = []
    for s, d, p in E:
        if p not in T[d]['w']:
            edge_bad.append((T[s]['n'], T[d]['n'], p))
    rp.add('每条血缘边的过程都是目标表的写入方', not edge_bad,
           f'不一致 {len(edge_bad)} 处' + (f'，如 {edge_bad[:2]}' if edge_bad else ''))

    no_up = []
    for i, t in enumerate(T):
        if not t['w']:
            continue
        if not any(d == i for s, d, p in E):
            no_up.append(t['n'])
    rp.add('有写入方但静态解析不到上游的表（信息项，多为临时表中转）', True,
           f'{len(no_up)} 张' + (f'，如 {no_up[:3]}' if no_up else '（全部有上游）'))

    # ---------- 7) 金标准（可选）----------
    if args.expect:
        exp = json.load(open(args.expect, encoding='utf-8'))
        idx = {t['n']: i for i, t in enumerate(T)}
        fails = []
        for tname, ups in exp.items():
            if tname not in idx:
                fails.append(f'{tname}: 表不存在')
                continue
            actual = {T[s]['n'] for s, d, p in E if d == idx[tname]}
            lack = [u for u in ups if u not in actual]
            if lack:
                fails.append(f'{tname}: 缺 {lack}')
        rp.add('金标准样本比对', not fails, '；'.join(fails) if fails else f'{len(exp)} 条全部命中')
    else:
        rp.add('金标准样本比对', None, '未提供 --expect，跳过')

    return rp.done()


if __name__ == '__main__':
    sys.exit(main())
