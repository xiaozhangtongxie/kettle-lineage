# -*- coding: utf-8 -*-
"""lineage.json → 交互式数据流图 HTML（作业 → 存储过程/SQL 步骤 → 表，支持点表反查）

用法：
  python render_html.py --in lineage.json --out flow.html [--template ../assets/template.html]
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser(description='渲染 Kettle 数据流图')
    ap.add_argument('--in', dest='inp', required=True, help='lineage.json')
    ap.add_argument('--out', required=True, help='输出 html')
    ap.add_argument('--template', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'template.html'))
    args = ap.parse_args()

    with open(args.inp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    with open(args.template, 'r', encoding='utf-8') as f:
        html = f.read()

    payload = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    if '__DATA__' not in html:
        raise SystemExit('模板缺少 __DATA__ 占位符')
    html = html.replace('__DATA__', payload)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML: {args.out} | 表 {len(data.get("tables", []))} 张 · 边 {len(data.get("edges", []))} 条')


if __name__ == '__main__':
    main()
