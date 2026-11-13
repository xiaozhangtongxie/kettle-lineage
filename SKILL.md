---
name: kettle-lineage
description: 从 Kettle 作业文件（.kjb/.ktr）与存储过程导出中还原「调度作业 → 存储过程/SQL 步骤 → 表」三级血缘，输出交互式数据流图（支持点表反查上下游影响）、Excel 台账和可导入脑图的大纲 txt。库名自动探测，换项目无需改配置。适用于梳理 ETL 调度链路、定位某张表由谁产出、评估改表/改过程的影响面。
agent_created: true
---

# Kettle 血缘梳理

把散落的 Kettle 作业与数据库存储过程，还原成一张可点击、可反查的血缘网络。

**设计目标：零配置。** 给一个调度 bat（或一个目录），库名自动探测，不需要预先知道项目用了哪些 schema。

## 何时使用

- 梳理 ETL 整体调度链路（哪些作业、按什么顺序、调了哪些存储过程）
- 回答「这张表是谁写的 / 数据从哪来」
- 回答「改这张表 / 改这个存储过程，会影响哪些下游表和作业」
- 把链路交付成 Excel 台账、脑图大纲或可演示的网页

## 30 秒上手

手里有一个（或多个）调度 `bat`，直接：

```bash
python scripts/run_all.py --bat "路径/day_dms/sync_xxx.bat" --outdir output
```

就这一条。产物在 `output/`：

| 文件 | 内容 |
|---|---|
| `flow.html` | 交互式数据流图（双击打开，离线可用，点表可反查上下游） |
| `台账.xlsx` | 5 个 sheet：说明 / 作业明细 / 表反查 / 表级血缘 / 作业汇总 |
| `大纲.txt` | Tab 缩进大纲，可直接导入百度脑图 / XMind / Obsidian |
| `lineage.json` | 中间数据，改样式时不用重新解析 |

**目录下有一堆 bat 时**，不用一个个列：

```bash
python scripts/run_all.py --bat-dir "路径/etl_main/data_sync_new" --outdir output
```

自动扫目录下所有 `*.bat`（排除 `bak`/`copy`/`test`），每个 bat 成为一条链路。

**有存储过程导出时**（强烈建议，能把表血缘挖到底）：

```bash
python scripts/run_all.py --dump "路径/存储过程导出.txt" --bat-dir "路径" --outdir output
```

## 输入准备

| 输入 | 必需 | 说明 |
|---|---|---|
| 调度 `bat` | ✅ | 里面必须有 `Kitchen -file=xxx.kjb`，程序据此定位主作业 |
| `kjb` / `ktr` 文件 | ✅ | 与 bat 同级或上下 4 层目录内即可；bat 里写生产机绝对路径（`F:\...`）也没关系，按 basename 就近找 |
| 存储过程全量导出 | 可选但强烈建议 | 一个文本文件，含多个 `CREATE [OR REPLACE] PROCEDURE/FUNCTION`。pgAdmin「导出全部」/ `pg_dump -s -n ods` 都行。没有它，`CALL` 到的过程只显示名字，内部表血缘展开不出来 |

## 库名自动探测（不用配）

程序会从三个地方统计库名，**换任何项目都不用改代码或加参数**：

1. `.ktr/.kjb` 里 SQL 的 `FROM/JOIN/INTO/UPDATE 库.表` 位置
2. `.ktr` 的 `<schema>xxx</schema>` 标签（Kettle 显式配置，权重最高）
3. `--dump` 存储过程正文里的 `库.表`（有些业务库只在过程体里出现）

过滤掉误判的两类：
- **表.字段** 写法（`order_list.created_time`）→ 该名字在 SQL 里也当过裸表名，说明它是表不是库
- 库下的"表名"全是字段名（`xxx._time`/`_id`/`_code`）→ 判为字段引用

想确认探测到了什么，跑 `extract_lineage.py` 时会打印 `[探测] 自动识别库名 N 个: ...`。

**极少数情况**才需要手动干预：

```bash
--schema my_rare_db     # 补充一个探测不到的业务库
--allow-bare            # 保留不带库名的表（默认白名单放行常见前缀）
--no-schema-filter      # 彻底关掉库名校验（只看别名黑名单），结果会变宽
```

## 调用方式

**方式 A：一键（推荐）**

```bash
python scripts/run_all.py --dump 过程导出.txt --bat-dir 作业目录 --outdir output [--expect expect.json]
```

依次完成：抽取 → 自检 → 交互图 → Excel + 大纲。**自检有 FAIL 会中断**，不产出错误数据。

**方式 B：分步**（只重跑某一步时，避免重复解析大文件）

```bash
python scripts/extract_lineage.py --dump 过程导出.txt --bat xxx.bat --out output/lineage.json
python scripts/selftest.py       --lineage output/lineage.json --dump 过程导出.txt
python scripts/render_html.py    --in output/lineage.json --out output/flow.html
python scripts/export_reports.py --in output/lineage.json --xlsx output/台账.xlsx --txt output/大纲.txt
```

**方式 C：对话里调用**

- 「用 kettle-lineage 梳理 `D:/etl/data_sync_new` 下所有 bat 的血缘，输出 Excel 和交互图」
- 「`dw.fact_daily_summary` 是谁产出的？看它的上游」
- 「改 `ods.md_distributor` 会影响哪些作业和下游表？」

## 工作流（程序内部怎么走）

**入口 = 调度 bat**，顺着 Kettle 的真实启动顺序自动往下追：

```
bat
 └ 解析 Kitchen -file=xxx.kjb
   └ 主作业 kjb（读 <hops> 得到真实执行顺序，不是 XML 顺序）
     ├ JOB  → 递归进子作业 kjb，继续按 hops
     ├ TRANS → 进 .ktr，按步骤间 hops 排序
     └ SQL  → 解析该步骤 SQL 的读写表 / CALL 的过程
                └ 有 --dump 时展开过程体 → 表
```

链路名称取 bat 文件名（如 `sync_report_db_kettle_day_dms`）；一个 bat 调多个 Kitchen 会拆成 `xxx#1`、`xxx#2`。

## 自检（交付前必跑，run_all 已内置）

```bash
python scripts/selftest.py --lineage output/lineage.json --dump 过程导出.txt
```

11 项检查：边索引/自环/重复、孤立表、表名能在源文件找到、抽样过程读写能回溯到过程体、
引用过程都有定义、主作业入口覆盖率、每条边的过程都是目标表写入方、无上游表统计。

**带业务金标准最可靠**（挑 3~5 张你最熟的表，写出必须出现的上游表）：

```json
{"dw.fact_daily_summary": ["ods.trade_order_detail"]}
```

```bash
python scripts/selftest.py --lineage output/lineage.json --dump 过程导出.txt --expect expect.json
```

退出码 0 = 全通过，1 = 有 FAIL。有 FAIL 先修，再交付。

## 交付物说明

- **flow.html**：三栏（作业 → 过程/SQL 步骤 → 表）。点作业展开过程与表；点表进入「表反查」——左侧上游、右侧下游，可逐层下钻；右侧面板给出影响过程数、受影响作业（按执行顺序）、最终产出表、影响路径
- **台账.xlsx**：0-说明 / 1-作业明细 / 2-表反查 / 3-表级血缘 / 4-作业汇总，均带筛选与冻结首行
- **大纲.txt**：Tab 缩进，节点下用 `目标表：` / `来源表：` 标注方向

## 关键规则（改代码前必读）

详见 `references/lineage-rules.md`。要点：

1. 存储过程必须**按 SQL 语句级配对**（`split_statements` 按分号切，忽略引号/括号内的分号），不能用整个过程体的「读表 × 写表」——否则产生笛卡尔积假血缘
2. `CREATE [TEMP] TABLE ... AS SELECT` 必须计入写入，否则「先建临时表再写目标表」会断链
3. 临时表先建边再**图收缩**（源→tmp→目标 ⇒ 源→目标），最终不展示。临时表识别除了 `tmp_`/`temp_`/`t_N` 前缀，还包括 SQL 里 `CREATE TEMPORARY TABLE` 显式声明的名字（如 `pm_check_result`）
4. Kettle `TableOutput` 没有 SQL，要沿 `.ktr` 的 `<order><hop>` 反向回溯到 `TableInput` 取源表
5. 每个内联 SQL 步骤独立成节点（`（SQL·作业名｜步骤名）`），跨作业合并会串线
6. 裸表名归一到带库名表时，按「层级优先 + 出现频次」选库，不能按字母序（否则 `customers` 会串到 `dbo.customers`）

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 作业数明显偏少 | bat 指向的是子作业；或 basename 多目录重名 | 传主调度 bat；必要时 `--job 名称=目录` |
| `[跳过] bat 引用的文件未找到` | kjb 不在 bat 同级/上级 4 层内 | 把 kjb 放到 bat 目录，或改用 `--job 名称=作业目录` |
| 表很少 / 大量表被丢 | 库名没探测到 | 看 `[探测]` 那行的库名列表；缺的用 `--schema` 补，或 `--no-schema-filter` 先看全貌 |
| 某张表上游为 0 | 表由动态 SQL / `${变量}` 产出 | 属已知局限，见 `references/lineage-rules.md` |
| 上游数量特别大（>100） | 该过程对很多表逐条 `INSERT ... SELECT`（如校验汇总） | 多为真实关系 |
| 中文路径报错 | Git Bash 路径转换 | 命令前加 `MSYS_NO_PATHCONV=1`，或用 PowerShell |
| 自检报「表名找不到」 | 表来自 Kettle 步骤配置而非过程体 | 检查 `.ktr` 的 TableOutput；必要时补 `BARE_OK` 前缀 |
