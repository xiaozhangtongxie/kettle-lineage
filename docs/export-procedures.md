# 怎么导出存储过程（标准输入）

`--dump` 要的是一份**纯文本文件**，里面按顺序放着所有存储过程/函数的**完整定义正文**。不需要表数据，`.sql` / `.txt` 都行，UTF-8 编码。

## 需要的格式长什么样

```sql
CREATE OR REPLACE PROCEDURE ods.sp_ods_orders() LANGUAGE plpgsql AS $procedure$
BEGIN
    INSERT INTO ods.orders SELECT * FROM erp_main.orders;
END;
$procedure$;

CREATE OR REPLACE PROCEDURE ods.sp_fact_sales() LANGUAGE plpgsql AS $procedure$
BEGIN
    INSERT INTO dw.fact_sales SELECT ... FROM ods.orders;
END;
$procedure$;
```

只要满足三条：

1. 每个过程以 `CREATE ... PROCEDURE/FUNCTION 名字` 开头，单独占一行
2. **必须有完整正文**（`AS $$ ... $$` 或 `BEGIN ... END`），只有名字清单不行
3. UTF-8 编码

---

## 最省事：DBeaver（PG / SQL Server / MySQL / Oracle 都通用）

1. 左侧导航树展开到 **Procedures**（函数在 **Functions** 下）
2. **右键选中**要导的过程 —— 可以 Ctrl 多选，也可以直接右键整个 `Procedures` 文件夹
3. 选 **SQL Editor → Generate SQL → DDL**
   （老版本 DBeaver 是右键 → **Generate SQL** → **DDL**）
4. 弹出的脚本窗口里 **Ctrl+A 全选 → 另存为** `procedures.sql`

DBeaver 会按你连接的数据库自动生成对应方言的 DDL，所以**这一套操作在 PG / SQL Server / MySQL / Oracle 上完全一样**，不用记命令。

> 想一次导出整个 schema 的所有过程：直接右键 schema 下的 `Procedures` 文件夹 → 同样操作。

---

## PostgreSQL

**命令行（pg_dump）**

```bash
pg_dump -h 主机 -p 5432 -U 用户名 -d 数据库名 --schema-only -n ods -f procedures.sql
```

- `-n ods` 只导这个 schema；多个就写多次：`-n ods -n dw -n dm`
- 不写 `-n` 就导全库，文件大一些但也能用
- 会顺带包含表、视图的 DDL，**不影响解析**（程序只认 `CREATE PROCEDURE/FUNCTION`）

**pgAdmin 图形界面**

1. 右键目标 schema → **Backup...**
2. 切到 **Objects** 标签页，只勾 **Functions**（PG 11+ 的存储过程也归在这里）
3. Format 选 **Plain**，Filename 填 `procedures.sql` → Backup

**SQL 查询（只要过程）**

```sql
SELECT pg_get_functiondef(p.oid) || ';'
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('ods')
  AND p.prokind IN ('p', 'f')      -- p=存储过程, f=函数
ORDER BY n.nspname, p.proname;
```

把结果里的单列内容复制出来存成 `.sql`（去掉表头和引号）。

---

## SQL Server

**SSMS 生成脚本**

1. 右键数据库 → **Tasks** → **Generate Scripts...**
2. Choose Objects → 选 **Stored Procedures**（函数在 User-defined Functions 里）
3. Advanced → **Types of data to script** 设为 **Schema only**
4. 输出选 **Save to file** → 单个文件 → `procedures.sql`

导出的内容形如 `CREATE PROCEDURE [dbo].[sp_x] ... GO`，程序会自动把 `[dbo].[sp_x]` 归一成 `dbo.sp_x`。

**T-SQL 查询**

```sql
SELECT m.definition
FROM sys.sql_modules m
JOIN sys.objects o ON m.object_id = o.object_id
WHERE o.type IN ('P', 'FN', 'IF', 'TF')   -- P=存储过程，FN/IF/TF=函数
ORDER BY o.type, o.name;
```

SSMS 里先切到「Results to Text」（`Ctrl+T`）再执行，导出更干净。

**命令行**

```bash
pip install mssql-scripter
mssql-scripter -S 主机 -d 数据库名 -U 用户名 -P 密码 \
               --include-objects StoredProcedures,UserDefinedFunctions \
               > procedures.sql
```

---

## MySQL

```bash
mysqldump -h 主机 -P 3306 -u root -p \
          --routines --no-create-info --no-data \
          数据库名 > procedures.sql
```

**`--routines` 必须加**，否则存储过程和函数根本不会导出。

导出的形如 ``CREATE DEFINER=`root`@`%` PROCEDURE `sp_x`()``，程序会自动跳过 `DEFINER`、把 `` `db`.`sp_x` `` 归一成 `db.sp_x`。

单个过程：`SHOW CREATE PROCEDURE db.sp_x;`

---

## Oracle

`all_source` 里每行一条记录，拼起来比较麻烦，建议用图形工具：

- **SQL Developer**：右键过程 → **Export**
- 或 `expdp` 加 `CONTENT=METADATA_ONLY`

---

## 导出后自检

数一下文件里有多少个 `CREATE ... PROCEDURE/FUNCTION`：

```bash
# Linux / macOS / Git Bash
grep -cE "CREATE +(OR +REPLACE +)?(DEFINER +\S+ +)?(PROCEDURE|FUNCTION|PROC) " procedures.sql
```

```powershell
# Windows PowerShell
(Select-String -Path procedures.sql -Pattern "CREATE +(OR +REPLACE +)?(PROCEDURE|FUNCTION|PROC) ").Count
```

数字应该和库里过程数量对得上。差很多通常是：

- MySQL 漏了 `--routines`
- PG 的 `-n schema` 写错
- 只导了名字清单，没导正文（`head -40 procedures.sql` 看不到 `BEGIN` 或 `AS $$` 就是这种）

跑完整流程时，程序会告诉你链路里引用了多少过程、其中多少个在导出文件里找不到定义 —— 找不到的会以「未解析过程」节点标出来，一眼可见。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 解析出 0 个过程 | 文件里只有名字清单，没正文 | 重新导出，确认含 `BEGIN...END` / `AS $$` |
| MySQL 一个都没识别 | 漏了 `--routines` | 加上重新导 |
| 过程识别到了但表血缘为空 | 过程体是动态 SQL / `${变量}` 表名 | 静态解析的固有限制，见 README「已知局限」 |
| 中文注释乱码 | 编码不是 UTF-8 | 导出时指定 UTF-8 |
| 文件太大（几十 MB） | 全库导出含表结构 | 用 `-n schema` 只导需要的 schema，解析快很多 |
