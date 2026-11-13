# 怎么导出存储过程（标准输入）

`--dump` 要的是一份**纯文本文件**，里面按顺序放着所有存储过程/函数的**完整定义正文**。不需要表数据，也不需要关心文件扩展名（`.sql` / `.txt` 都行）。

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

要点只有三条：

1. 每个过程以 `CREATE ... PROCEDURE/FUNCTION 名字` 开头，单独占一行
2. **必须有完整正文**（`AS $$ ... $$` 或 `BEGIN ... END`），只有名字清单不行
3. UTF-8 编码

程序已兼容各家写法：

| 写法 | 支持 |
|---|---|
| PG / Oracle：`CREATE OR REPLACE PROCEDURE ods.sp_x()` | ✅ |
| SQL Server：`CREATE PROCEDURE [dbo].[sp_x]` | ✅ 自动归一为 `dbo.sp_x` |
| MySQL：``CREATE DEFINER=`root`@`%` PROCEDURE `db`.`sp_x`()`` | ✅ 自动去掉 DEFINER 和反引号 |
| PG：`CREATE FUNCTION` / SQL Server：`CREATE PROC` | ✅ |
| 调用：`CALL x()` / `EXEC dbo.x` / `EXECUTE x` | ✅ |
| `EXEC('动态SQL')` | ❌ 无法静态解析 |

---

## PostgreSQL（pg_dump，推荐）

```bash
pg_dump -h 主机 -p 5432 -U 用户名 -d 数据库名 \
        --schema-only -n ods -f procedures.sql
```

- `-n ods` 只导这个 schema；有多个就写多次：`-n ods -n dw -n dm`
- 不写 `-n` 就导全库，文件会大一些，但一样能用
- `--schema-only` 表示只导结构不导数据
- 会顺带包含表、视图的 DDL，**不影响解析**（程序只认 `CREATE PROCEDURE/FUNCTION`）

如果连接需要密码，用 `PGPASSWORD=xxx pg_dump ...`，或配置 `~/.pgpass`。

## PostgreSQL（pgAdmin 图形界面）

1. 右键目标 schema → **Backup...**
2. 切到 **Objects** 标签页，只勾 **Functions**（Procedures 在 PG 11+ 归在此类）
3. Format 选 **Plain**，Filename 填 `procedures.sql`
4. 点 Backup

## PostgreSQL（SQL 查询，适合只要过程不要其他）

```sql
SELECT pg_get_functiondef(p.oid) || ';'
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname IN ('ods')
  AND p.prokind IN ('p', 'f')      -- p=存储过程, f=函数
ORDER BY n.nspname, p.proname;
```

把结果网格里的**单列内容**（不带表头、不带引号）复制出来存成 `.sql`。
pgAdmin 里更省事：结果网格 → 右上角 **Download as CSV**，再把首尾的引号去掉；或者直接用「Query Tool → Save Results」。

---

## SQL Server（SSMS 生成脚本，推荐）

1. 右键数据库 → **Tasks** → **Generate Scripts...**
2. 「Choose Objects」→ 选 **Stored Procedures**（函数在 User-defined Functions 里）
3. 「Set Scripting Options」→ **Advanced** → 找到 **Types of data to script**，设为 **Schema only**
4. 输出选 **Save to file** → 单个文件 → 保存为 `procedures.sql`

这样导出的文件里每个过程是 `CREATE PROCEDURE [dbo].[sp_x] ... GO`，程序能直接识别。

## SQL Server（T-SQL 查询）

```sql
SELECT m.definition
FROM sys.sql_modules m
JOIN sys.objects o ON m.object_id = o.object_id
WHERE o.type IN ('P', 'FN', 'IF', 'TF')   -- P=存储过程，FN/IF/TF=函数
ORDER BY o.type, o.name;
```

SSMS 里执行后，**右键结果网格 → Save Results As...** → 存成 `.rpt` 或 `.txt` 再改后缀。
⚠️ 默认导出会带列头和每行引号，导出前先把查询改成「Results to Text」（`Ctrl+T`）更干净。

## SQL Server（命令行）

```bash
# 需要先装：pip install mssql-scripter
mssql-scripter -S 主机名 -d 数据库名 -U 用户名 -P 密码 \
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
`--no-data` 不导数据，`--no-create-info` 不导表结构（只留过程）。

导出的文件是 ``CREATE DEFINER=`root`@`%` PROCEDURE `sp_x`()`` 形式，程序会自动跳过 `DEFINER` 并把 `` `db`.`sp_x` `` 归一为 `db.sp_x`。

## MySQL（单个过程）

```sql
SHOW CREATE PROCEDURE db.sp_x;
```

---

## Oracle

```sql
SELECT text
FROM all_source
WHERE owner = 'ODS' AND type = 'PROCEDURE'
ORDER BY name, line;
```

这种方式每行一条记录，需要按 `name` 拼回去，比较麻烦。建议用 **SQL Developer → 右键过程 → Export**，或 `expdp` 的 `CONTENT=METADATA_ONLY`。

---

## 导出后自检

先数一下文件里有多少个 `CREATE ... PROCEDURE/FUNCTION`：

```bash
# Linux / macOS / Git Bash
grep -cE "CREATE +(OR +REPLACE +)?(DEFINER +\S+ +)?(PROCEDURE|FUNCTION|PROC) " procedures.sql
```

```powershell
# Windows PowerShell
(Select-String -Path procedures.sql -Pattern "CREATE +(OR +REPLACE +)?(DEFINER +\S+ +)?(PROCEDURE|FUNCTION|PROC) ").Count
```

这个数字应该和你库里过程的数量对得上。差很多就说明导出方式不对（最常见的是漏了 `--routines` 或 `-n schema`）。

再确认正文在（不是只有名字）：

```bash
head -40 procedures.sql          # 能看到 BEGIN ... END 或 AS $$ 就对了
```

跑一遍完整流程时，`extract_lineage.py` 会打印链路中引用了多少个过程、其中多少个在导出文件里找不到定义 —— 找不到的会以「未解析过程」节点标出来，一眼可见。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 解析出 0 个过程 | 文件里只有过程名清单，没有正文 | 重新导出，确认含 `BEGIN...END` / `AS $$` |
| MySQL 一个都没识别 | 漏了 `--routines` | 加上重新导 |
| SQL Server 过程名带 `[dbo].[sp_x]` | 老版本未做归一 | 升级到当前版本即可（已自动去方括号） |
| 过程识别到了但表血缘为空 | 过程体里是动态 SQL / `${变量}` 表名 | 静态解析的固有限制，见 README「已知局限」 |
| 中文注释变乱码 | 编码不是 UTF-8 | 导出时指定 `--encoding=UTF8`（PG）或另存为 UTF-8 |
| 文件太大（几十 MB） | 全库导出含表结构 | 用 `-n schema` 只导需要的 schema，解析速度会快很多 |
