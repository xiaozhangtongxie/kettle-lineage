# 血缘解析规则与踩坑记录

## 1. 数据模型

```
lineage.json
├── chains[]            调度链（一个 .bat / 主作业一条）
│   └── jobs[]          主作业里的一级节点（JOB / TRANS / SQL 入口）
│       └── procs[]     该入口下的存储过程 或 内联 SQL 步骤节点
│           ├── n       名称（过程名，或 （SQL·作业｜步骤名））
│           ├── w / r   写/读 的表数量
│           └── t[]     {n: 表名, l: 层, d: 'w'|'r'|'wr'}
├── tables[]            {n, l, w:[写入过程], r:[读取过程]}
├── edges[]             [源表索引, 目标表索引, 过程名]
└── procJobs{}          过程 → [[链路, 作业, 序号], ...]
```

`l` 层级取值：`src`（外部系统库）、`ods`、`dw`、`dm`、`his`、`misc`；`src` 包含 `erp_*`、`crm*`、`ods_legacy`、`company*` 等库名。

## 2. 存储过程解析

### 2.1 语句级配对（最重要）

```
错误做法：proc 全部 reads × proc 全部 writes
   → sp_ods_to_dm 有 40 条 INSERT、读了 109 张表 → 4360 条假边

正确做法：按 ';' 切分成语句，每条语句内部的 reads × writes
   → 40 条 INSERT 各自只连自己的来源表
```

`split_statements` 用一个字符扫描器，跳过单/双引号内的分号、括号内的分号。不要用 `re.split(';')`。

### 2.2 写入模式

```python
INSERT INTO x
UPDATE x            # 注意排除 UPDATE( 函数调用
DELETE FROM x
TRUNCATE [TABLE] x
MERGE INTO x
CREATE [TEMP|TEMPORARY|UNLOGGED] TABLE [IF NOT EXISTS] x   # CTAS，必须计入
```

CTAS 的坑：ETL 过程常见写法

```sql
create temp table temp_step_three as select ... from ods.a join ods.b ...;
insert into dw.fact_x select ... from temp_step_three;
```

若不把 CTAS 当写入，`temp_step_three` 没有入边，第二句的来源被丢弃 → 目标表上游变 0。

### 2.3 临时表收缩

临时表先作为普通节点建边，再在图上收缩：

```
ods.a → temp_x → dw.fact   ⇒   ods.a → dw.fact   （标签取写出 dw.fact 的过程）
```

收缩后删除所有临时表节点，避免用户在图上看到一堆 `tmp_`/`temp_` 噪音；同时保留真实链路。

### 2.4 表名归一

| 情况 | 处理 |
|---|---|
| `report_db.ods.store`（三段式，跨库引用） | 取后两段 → `ods.store` |
| 裸表名 `store`（过程内未写库名） | 若全量里存在唯一的 `库.store`，合并过去；否则保留原样 |
| `pg_*`、`information_schema.*` | 丢弃（系统对象） |
| `log.*`、`sp_*_log` / `sp_*_logs` 结尾的日志过程 | 丢弃（调度日志噪音） |
| 含 `${...}` 的表名 | 丢弃（Kettle 参数化表，静态无法确定） |

库名白名单见 `extract_lineage.py` 的 `KNOWN_SCHEMAS`，新增库请补充，否则该库的表会被判为非法名丢弃。

## 3. Kettle 解析

- **kjb 顺序**：按 `<hops>` 从 `Start` 做 BFS，得到真实执行顺序；跳过 `SPECIAL/SUCCESS/MAIL/ADD_RESULT_FILENAMES`
- **变量替换**：`${Internal.Entry.Current.Directory}` → 当前 kjb 所在目录
- **Transformation**：`<step>` 里的 `<sql>`（TableInput/ExecSQL）直接解析；`TableOutput` 无 SQL，需回溯：

```
TableOutput(table=t)
  ← 沿 <order><hop> 反向 BFS（最多 6 跳）
     ← 命中的 TableInput：取 <table> 或 SQL 里的 FROM 表作为源
```

- **内联 SQL 步骤**：每个步骤生成独立节点，名称 `（SQL·<入口作业>｜<步骤名>）`。**不要**把所有作业的内联 SQL 合成一个「（步骤内联 SQL）」节点——会造成跨作业串线，出现「点一张表冒出 49 张上游、9 个作业」的假象。

## 4. 已知局限

1. `EXECUTE 'insert into ...'` 这类动态 SQL 无法解析（字符串拼接）
2. Kettle 参数化表名 `insert into ods.${table_name}` 无法展开
3. 视图（`v_*`）按普通表处理，不展开到基表
4. 只分析传入的 `--job` 目录，其他调度目录（如 1h/1month/real 增量）不会自动纳入
5. 存储过程导出若不含某过程定义，该过程只作为节点存在，无读写表

## 5. 验收清单

跑完后至少核对：

- [ ] 作业数与主作业实际一级节点数一致（用 Spoon 打开主 kjb 对照）
- [ ] 任选一张已知的 fact 表，上游表数与存储过程里该 INSERT 语句的 FROM/JOIN 表数量接近
- [ ] 无自环（`s == d`）、无越界索引、无重复 `(s,d,p)`
- [ ] 裸表名数量接近 0（说明归一规则覆盖）
- [ ] 页面上点表反查时「写入过程」不是 `（步骤内联 SQL）` 这种聚合节点

## 6. 如何验证结果可信

分三层验证，从便宜到贵：

**① 结构自洽（脚本自动，10 秒）**

```bash
python scripts/selftest.py --lineage output/lineage.json --dump procedures.txt
```

关注两项：
- `每条血缘边的过程都是目标表的写入方` —— 失败说明配对或收缩有 bug
- `有写入方但静态解析不到上游的表` —— 信息项，数量占比高时说明大量过程走临时表/动态 SQL

**② 源码回溯（脚本自动抽样 40 个过程）**

校验「血缘里出现的每张表，都能在该过程的 SQL 文本里找到」，防止正则凭空造表。

**③ 业务金标准（人工，最可靠）**

挑 3~5 张自己最熟的报表表，打开它的存储过程，手数该表 INSERT 语句的 FROM/JOIN 表，
写成 `expect.json`，用 `--expect` 跑。命中率 100% 才能认为链路可信。

**手工抽查方法（不写代码）**

1. 在页面上点一张 dm 层表 → 「写入过程」记下过程名
2. 打开该过程的定义，搜 `insert into <表名>`，把它下面 SELECT 的 FROM 表抄下来
3. 与页面上「上游第 1 层」对比，应当基本一致（差异来自 CTAS 临时表中转，会被收缩掉）


## 7. 库名自动探测（v2 起，替代写死白名单）

早期版本把项目库名写死在 KNOWN_SCHEMAS 里，换项目必须加 --no-schema-filter，否则表被大量丢弃。
现在改为自动探测，三处语料 + 三层过滤：

### 语料
1. .ktr/.kjb 里 SQL 的 `FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE 库.表` 位置
2. .ktr 的 `<schema>xxx</schema>`（Kettle 显式配置）
3. --dump 存储过程正文（有些业务库只在过程体出现）

### 过滤（关键，全部踩过坑）
| 问题 | 例子 | 处理 |
|---|---|---|
| 只扫 Kettle 漏库名 | dw_his、jsb_sfa_* 只在过程体里 | dump 纳入语料（否则边数 1345→1179） |
| 表.字段被当库.表 | `order_list.created_time` / `.delivery_time` 凑够 2 个表名 | 该名字在 SQL 里也当过裸表名 → 排除 |
| 字段名凑数 | `xxx._time` / `_id` / `_code` | 库下表名全是字段名 → 排除 |
| 过严漏真库 | marketing_dev 下只有 1 张表 | 唯一表名不像字段名 → 保留 |

### 回归基线（改这里必跑）
- 项目A（SQLServer 源 + PG 数仓）：501 表 / 1345 边（与原写死白名单版本边数一致）
- 项目B（纯 Kettle，无存储过程）：yun_etl 单 bat 6 表 4 边；--bat-dir 全扫 9 链 39 作业 58 表 57 边
