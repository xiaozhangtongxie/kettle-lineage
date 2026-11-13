# -*- coding: utf-8 -*-
"""一键跑完：抽取 → 自检 → 交互图 → Excel + 大纲

用法：
  python run_all.py --dump 过程导出.txt \
      --bat day_dms/sync_report_db_kettle_day_dms.bat \
      --bat day_sfa/sync_report_db_kettle_day_sfa.bat \
      --outdir output [--expect expect.json] [--skip-test]

产物（outdir 下）：
  lineage.json                     中间数据
  flow.html                        交互式数据流图（点表反查）
  台账.xlsx                         Excel 台账（5 个 sheet）
  大纲.txt                           Tab 缩进大纲（可导入脑图）
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd):
    print('\n$ ' + ' '.join(f'"{c}"' if ' ' in str(c) else str(c) for c in cmd[1:]))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f'命令失败（退出码 {r.returncode}）')


def main():
    ap = argparse.ArgumentParser(description='Kettle 血缘一键流程')
    ap.add_argument('--dump', help='存储过程全量导出文件（可省略：只做 Kettle 侧血缘）')
    ap.add_argument('--bat', action='append', default=[], help='调度入口 bat，可重复')
    ap.add_argument('--bat-dir', action='append', default=[], metavar='DIR',
                    help='目录：自动扫描其中所有同步类 bat 作为入口，可重复（新人最省事）')
    ap.add_argument('--job', action='append', default=[], help='（兼容）名称=作业目录')
    ap.add_argument('--outdir', default='output', help='输出目录（默认 output）')
    ap.add_argument('--expect', help='金标准 JSON（可选）')
    ap.add_argument('--skip-test', action='store_true', help='跳过自检')
    ap.add_argument('--schema', action='append', default=[], help='补充合法库名，可重复')
    ap.add_argument('--no-schema-filter', action='store_true', help='不校验库名白名单（新项目）')
    ap.add_argument('--allow-bare', action='store_true', help='保留不带库名的表（新项目）')
    args = ap.parse_args()

    if not args.bat and not args.job and not args.bat_dir:
        print('至少要给一个 --bat / --bat-dir（或 --job）')
        return 2

    out = os.path.abspath(args.outdir)
    os.makedirs(out, exist_ok=True)
    py = sys.executable
    lineage = os.path.join(out, 'lineage.json')

    extract = [py, os.path.join(HERE, 'extract_lineage.py'), '--out', lineage]
    if args.dump:
        extract += ['--dump', args.dump]
    for b in args.bat:
        extract += ['--bat', b]
    for d in args.bat_dir:
        extract += ['--bat-dir', d]
    for j in args.job:
        extract += ['--job', j]
    for sc in args.schema:
        extract += ['--schema', sc]
    if args.no_schema_filter:
        extract.append('--no-schema-filter')
    if args.allow_bare:
        extract.append('--allow-bare')
    run(extract)

    if not args.skip_test:
        test = [py, os.path.join(HERE, 'selftest.py'), '--lineage', lineage]
        if args.dump:
            test += ['--dump', args.dump]
        if args.expect:
            test += ['--expect', args.expect]
        run(test)

    run([py, os.path.join(HERE, 'render_html.py'), '--in', lineage,
         '--out', os.path.join(out, 'flow.html')])
    run([py, os.path.join(HERE, 'export_reports.py'), '--in', lineage,
         '--xlsx', os.path.join(out, '台账.xlsx'), '--txt', os.path.join(out, '大纲.txt')])

    print('\n完成，产物目录：' + out)
    for f in ('flow.html', '台账.xlsx', '大纲.txt'):
        p = os.path.join(out, f)
        if os.path.exists(p):
            print(f'  {f:12s} {os.path.getsize(p) / 1024:.0f} KB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
