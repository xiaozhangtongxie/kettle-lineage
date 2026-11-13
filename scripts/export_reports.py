# -*- coding: utf-8 -*-
"""lineage.json → Excel 台账（多 sheet）+ Tab 缩进大纲 txt（可导入脑图工具）

用法：
  python export_reports.py --in lineage.json --xlsx 台账.xlsx --txt 大纲.txt
"""
import argparse
import json
import os


def build_rows(data):
    """作业明细 / 表反查 / 表级血缘 / 作业汇总"""
    T, E = data['tables'], data['edges']
    proc_jobs = data.get('procJobs', {})

    node_rows, job_summary = [], []
    for ch in data['chains']:
        for job in ch['jobs']:
            tbls, nproc, nstep = set(), 0, 0
            for node in job['procs']:
                is_step = node['n'].startswith('（SQL·')
                nstep += is_step
                nproc += (not is_step)
                for e in node['t']:
                    tbls.add(e['n'])
                node_rows.append((ch['name'], job['seq'], job['name'],
                                  'SQL 步骤' if is_step else '存储过程', node['n'],
                                  node['w'], node['r']))
            job_summary.append((ch['name'], job['seq'], job['name'], nproc, nstep, len(tbls)))

    in_up, in_dn = {}, {}
    for s, d, p in E:
        in_up.setdefault(d, set()).add(T[s]['n'])
        in_dn.setdefault(s, set()).add(T[d]['n'])

    table_rows = []
    for i, t in enumerate(T):
        jobs = sorted({(j[0], j[2], j[1]) for p in (t['w'] + t['r']) for j in proc_jobs.get(p, [])})
        table_rows.append((t['n'], t['l'], len(t['w']), '、'.join(t['w']) or '—',
                           len(t['r']), '、'.join(t['r']) or '—',
                           len(in_up.get(i, ())), len(in_dn.get(i, ())),
                           '；'.join(f'{j[0]} {str(j[1]).zfill(2)} {j[2]}' for j in jobs) or '—'))

    edge_rows = []
    for s, d, p in E:
        js = proc_jobs.get(p, [])
        loc = f'{js[0][0]} {str(js[0][2]).zfill(2)} {js[0][1]}' if js else '—'
        edge_rows.append((T[s]['n'], T[d]['n'], p, loc))
    return node_rows, table_rows, edge_rows, job_summary


def write_xlsx(data, path, node_rows, table_rows, edge_rows, job_summary):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    hdr_fill = PatternFill('solid', fgColor='1F3A5F')
    hdr_font = Font(color='FFFFFF', bold=True, size=10)

    def sheet(title, headers, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for c in ws[1]:
            c.fill = hdr_fill
            c.font = hdr_font
            c.alignment = Alignment(vertical='center', horizontal='center')
        for r in rows:
            ws.append(list(r))
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions

    ws0 = wb.create_sheet('0-说明')
    info = [
        ('梳理口径', 'Kettle 文件（bat→kjb→entry→ktr→步骤）+ 存储过程体解析；输出 作业→存储过程/SQL步骤→表 三级链路'),
        ('血缘取法①', '存储过程：按过程体内每条 SQL 语句配对——该语句的 FROM/JOIN 表 → 该语句的写入目标表'),
        ('血缘取法②', 'Kettle 转换：TableOutput 沿 ktr 内部 hops 反向回溯到 TableInput，取其表/SQL 读取表作为源'),
        ('血缘取法③', '临时表（tmp_/temp_/t_N）先建边再图收缩（源→tmp→目标 ⇒ 源→目标），最终不展示'),
        ('表名归一', 'report_db.ods.x → ods.x；无库名表按同名唯一匹配合并'),
        ('统计', f"作业 {len(job_summary)} 个 · 节点 {len(node_rows)} 个 · 表 {len(data['tables'])} 张 · 血缘 {len(data['edges'])} 条"),
        ('过滤', '已过滤 pg_*/information_schema、log.* 表、日志过程、${变量} 表'),
        ('已知局限', 'EXECUTE 动态 SQL 无法解析；视图按表处理未展开基表；Kettle 参数化表名无法静态确定'),
        ('', ''),
        ('Sheet 1-作业明细', '每个作业下的存储过程/SQL 步骤及读写表数量'),
        ('Sheet 2-表反查', '每张表的写入/读取过程、上下游表数量、涉及作业'),
        ('Sheet 3-表级血缘', '源表 → 目标表，经由哪个过程/作业'),
        ('Sheet 4-作业汇总', '每个作业的过程数、步骤数、涉及表数'),
    ]
    for r in info:
        ws0.append(list(r))
    ws0.column_dimensions['A'].width = 24
    ws0.column_dimensions['B'].width = 110
    for row in ws0.iter_rows():
        row[0].font = Font(bold=True, color='1F3A5F')
        row[1].alignment = Alignment(wrap_text=True, vertical='top')

    sheet('1-作业明细', ['链路', '序号', '作业', '类型', '节点（过程/SQL步骤）', '写表数', '读表数'],
          node_rows, [30, 6, 34, 10, 56, 8, 8])
    sheet('2-表反查', ['表名', '层级', '写入过程数', '写入过程', '读取过程数', '读取过程', '上游表数', '下游表数', '涉及作业'],
          table_rows, [44, 8, 10, 46, 10, 46, 9, 9, 44])
    sheet('3-表级血缘', ['源表', '目标表', '经由过程/步骤', '所属作业'],
          sorted(edge_rows), [44, 44, 46, 30])
    sheet('4-作业汇总', ['链路', '序号', '作业', '过程数', 'SQL步骤数', '涉及表数'],
          job_summary, [30, 6, 34, 8, 10, 10])
    wb.save(path)


def write_outline(data, path):
    lines = ['生产作业流程图']
    for ch in data['chains']:
        lines.append('\t' + ch['name'])
        for job in ch['jobs']:
            lines.append('\t\t' + job['name'])
            for node in job['procs']:
                nm = node['n']
                if nm.startswith('（SQL·'):
                    nm = nm[nm.index('｜') + 1:].rstrip('）')
                    lines.append('\t\t\t[SQL步骤] ' + nm)
                else:
                    lines.append('\t\t\t' + nm + '()')
                for e in node['t']:
                    if 'w' in e['d']:
                        lines.append('\t\t\t\t目标表：' + e['n'])
                for e in node['t']:
                    if e['d'] == 'r':
                        lines.append('\t\t\t\t来源表：' + e['n'])
    with open(path, 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(lines) + '\n')
    return len(lines)


def main():
    ap = argparse.ArgumentParser(description='导出 Excel 台账与大纲 txt')
    ap.add_argument('--in', dest='inp', required=True)
    ap.add_argument('--xlsx', default=None)
    ap.add_argument('--txt', default=None)
    args = ap.parse_args()

    with open(args.inp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    node_rows, table_rows, edge_rows, job_summary = build_rows(data)

    if args.xlsx:
        d = os.path.dirname(os.path.abspath(args.xlsx))
        os.makedirs(d, exist_ok=True)
        write_xlsx(data, args.xlsx, node_rows, table_rows, edge_rows, job_summary)
        print('Excel:', args.xlsx)
    if args.txt:
        d = os.path.dirname(os.path.abspath(args.txt))
        os.makedirs(d, exist_ok=True)
        n = write_outline(data, args.txt)
        print('大纲:', args.txt, '| 行数', n)


if __name__ == '__main__':
    main()
