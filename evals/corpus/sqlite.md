# sqlite 文档（Context7 抓取，评估语料）


## Topic: transactions

### C: Nested Transactions with Savepoints

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Illustrates the use of savepoints in C for implementing nested transactions in SQLite. This allows for partial rollbacks within a larger transaction. Requires the SQLite3 library.

```c
// Savepoint example for nested transactions
int nested_transaction_example(sqlite3 *db) {
    sqlite3_exec(db, "BEGIN TRANSACTION", NULL, NULL, NULL);

    // Do some work
    sqlite3_exec(db, "INSERT INTO users (name, age) VALUES ('Alice', 25)", NULL, NULL, NULL);

    // Create savepoint
    sqlite3_exec(db, "SAVEPOINT sp1", NULL, NULL, NULL);

    // More work that might fail
    int rc = sqlite3_exec(db, "INSERT INTO users (name, age) VALUES ('Bob', 30)", NULL, NULL, NULL);

    if (rc != SQLITE_OK) {
        // Rollback to savepoint
        sqlite3_exec(db, "ROLLBACK TO sp1", NULL, NULL, NULL);
        printf("Rolled back to savepoint\n");
    } else {
        // Release savepoint
        sqlite3_exec(db, "RELEASE sp1", NULL, NULL, NULL);
    }

    // Commit outer transaction
    sqlite3_exec(db, "COMMIT", NULL, NULL, NULL);
    return SQLITE_OK;
}

```

--------------------------------

### C: Batch Inserts with Transactions

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates how to perform multiple insert operations within a single transaction in SQLite using C. It includes error handling for transaction begin, insert steps, and commit. Requires the SQLite3 library.

```c
#include <sqlite3.h>
#include <stdio.h>

int batch_insert_with_transaction(sqlite3 *db) {
    char *err_msg = NULL;
    int rc;

    // Begin transaction
    rc = sqlite3_exec(db, "BEGIN TRANSACTION", NULL, NULL, &err_msg);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Cannot begin transaction: %s\n", err_msg);
        sqlite3_free(err_msg);
        return rc;
    }

    // Prepare insert statement
    sqlite3_stmt *stmt;
    const char *sql = "INSERT INTO users (name, email, age) VALUES (?, ?, ?)";
    sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);

    // Insert multiple rows
    const char *users[][3] = {
        {"Alice", "alice@example.com", "25"},
        {"Bob", "bob@example.com", "30"},
        {"Charlie", "charlie@example.com", "35"}
    };

    for (int i = 0; i < 3; i++) {
        sqlite3_bind_text(stmt, 1, users[i][0], -1, SQLITE_STATIC);
        sqlite3_bind_text(stmt, 2, users[i][1], -1, SQLITE_STATIC);
        sqlite3_bind_int(stmt, 3, atoi(users[i][2]));

        rc = sqlite3_step(stmt);
        if (rc != SQLITE_DONE) {
            fprintf(stderr, "Insert failed: %s\n", sqlite3_errmsg(db));
            sqlite3_finalize(stmt);
            sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
            return rc;
        }

        sqlite3_reset(stmt);
    }

    sqlite3_finalize(stmt);

    // Commit transaction
    rc = sqlite3_exec(db, "COMMIT", NULL, NULL, &err_msg);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Cannot commit: %s\n", err_msg);
        sqlite3_free(err_msg);
        sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
        return rc;
    }

    printf("Transaction completed successfully\n");
    return SQLITE_OK;
}

```

--------------------------------

### SQLite CSV and Data Import/Export

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Provides bash scripts for importing data from CSV files into SQLite tables and exporting data from tables to CSV and JSON formats. Includes handling of headers and skipping rows.

```bash
# Create table for CSV import
sqlite3 mydb.db <<EOF
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    age INTEGER
);
EOF
```

```bash
# Import CSV with headers
sqlite3 mydb.db <<EOF
.mode csv
.import --csv users.csv users
EOF
```

```bash
# Import without headers (skip first line)
sqlite3 mydb.db <<EOF
.mode csv
.import --csv --skip 1 users.csv users
EOF
```

```bash
# Export to CSV
sqlite3 mydb.db <<EOF
.headers on
.mode csv
.output users_export.csv
SELECT * FROM users;
.output stdout
EOF
```

```bash
# Export to JSON
sqlite3 mydb.db <<EOF
.mode json
.output users.json
SELECT * FROM users;
.output stdout
EOF
```

```bash
# Bulk operations with transaction
sqlite3 mydb.db <<EOF
BEGIN TRANSACTION;
.mode csv
.import large_data.csv staging_table
INSERT INTO users SELECT * FROM staging_table WHERE age >= 18;
DROP TABLE staging_table;
COMMIT;
EOF
```

--------------------------------

### Perform Hot Backup of SQLite Database

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

This C code demonstrates how to use the SQLite backup API to copy a database file while it is in use. It opens a destination database, initializes the backup process, and performs the backup step-by-step with progress monitoring. It also includes a function to back up to an in-memory database.

```c
#include <sqlite3.h>
#include <stdio.h>

int backup_database(sqlite3 *pSource, const char *zDestFile) {
    sqlite3 *pDest;
    sqlite3_backup *pBackup;
    int rc;

    // Open destination database
    rc = sqlite3_open(zDestFile, &pDest);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Cannot open destination database: %s\n", sqlite3_errmsg(pDest));
        sqlite3_close(pDest);
        return rc;
    }

    // Initialize backup
    pBackup = sqlite3_backup_init(pDest, "main", pSource, "main");
    if (pBackup == NULL) {
        fprintf(stderr, "Cannot initialize backup: %s\n", sqlite3_errmsg(pDest));
        sqlite3_close(pDest);
        return SQLITE_ERROR;
    }

    // Perform backup with progress monitoring
    do {
        rc = sqlite3_backup_step(pBackup, 100);  // Copy 100 pages at a time

        int remaining = sqlite3_backup_remaining(pBackup);
        int total = sqlite3_backup_pagecount(pBackup);
        printf("Backup progress: %d/%d pages\r", total - remaining, total);
        fflush(stdout);

        if (rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED) {
            sqlite3_sleep(100);  // Sleep 100ms before retry
        }
    } while (rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED);

    printf("\n");

    // Finalize backup
    sqlite3_backup_finish(pBackup);

    if (rc == SQLITE_DONE) {
        rc = SQLITE_OK;
        printf("Backup completed successfully\n");
    } else {
        fprintf(stderr, "Backup failed: %s\n", sqlite3_errmsg(pDest));
    }

    sqlite3_close(pDest);
    return rc;
}

// Backup to in-memory database
int backup_to_memory(sqlite3 *pFile, sqlite3 **ppMemory) {
    int rc;
    sqlite3_backup *pBackup;

    rc = sqlite3_open(":memory:", ppMemory);
    if (rc != SQLITE_OK) return rc;

    pBackup = sqlite3_backup_init(*ppMemory, "main", pFile, "main");
    if (pBackup == NULL) {
        sqlite3_close(*ppMemory);
        return SQLITE_ERROR;
    }

    rc = sqlite3_backup_step(pBackup, -1);  // Copy entire database
    sqlite3_backup_finish(pBackup);

    return rc == SQLITE_DONE ? SQLITE_OK : rc;
}

```

### Technical and Design Documentation > How SQLite Implements Atomic Commit

Source: https://www.sqlite.org/docs.html

SQLite implements atomic commit logic to ensure transaction integrity, maintaining consistency even in the event of power failures.

## Topic: indexes

### Create and Query R-Tree Spatial Indexes in SQLite

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates the creation and usage of R-Tree virtual tables in SQLite for efficient multi-dimensional range queries. Includes examples for inserting points and rectangles, performing range searches, and finding overlapping shapes.

```sql
-- Create R-Tree index for 2D points
CREATE VIRTUAL TABLE locations_idx USING rtree(
    id,              -- Integer primary key
    min_x, max_x,    -- X-axis bounds
    min_y, max_y     -- Y-axis bounds
);
```

```sql
-- Create R-Tree for rectangles
CREATE VIRTUAL TABLE buildings USING rtree(
    building_id,
    min_x, max_x,
    min_y, max_y
);
```

```sql
-- Insert point data (for points, min equals max)
INSERT INTO locations_idx VALUES (1, -73.9857, -73.9857, 40.7484, 40.7484);  -- Empire State Building
INSERT INTO locations_idx VALUES (2, -73.9851, -73.9851, 40.7580, 40.7580);  -- Times Square
```

```sql
-- Insert rectangle data
INSERT INTO buildings VALUES (1, 10.0, 20.0, 10.0, 20.0);
INSERT INTO buildings VALUES (2, 15.0, 25.0, 15.0, 25.0);
```

```sql
-- Range query: find points within bounds
SELECT id FROM locations_idx
WHERE min_x >= -74.0 AND max_x <= -73.9
  AND min_y >= 40.7 AND max_y <= 40.8;
```

```sql
-- Find overlapping rectangles
SELECT building_id FROM buildings
WHERE max_x >= 12.0 AND min_x <= 18.0
  AND max_y >= 12.0 AND min_y <= 18.0;
```

```sql
-- Find buildings containing a point
SELECT building_id FROM buildings
WHERE min_x <= 17.5 AND max_x >= 17.5
  AND min_y <= 17.5 AND max_y >= 17.5;
```

```sql
-- Nearest neighbor search (SQLite 3.24.0+)
SELECT id,
       (max_x - (-73.9857)) * (max_x - (-73.9857)) +
       (max_y - 40.7484) * (max_y - 40.7484) AS distance_squared
FROM locations_idx
WHERE min_x >= -74.1 AND max_x <= -73.8
  AND min_y >= 40.6 AND max_y <= 40.9
ORDER BY distance_squared
LIMIT 10;
```

--------------------------------

### SQLite SQL Syntax and Data Type Examples

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates SQLite's SQL dialect, including table creation with manifest typing, STRICT tables, generated columns, WITHOUT ROWID optimization, expression indexes, partial indexes, and row value comparisons. It showcases how SQLite associates types with values rather than columns.

```sql
-- Creating tables with manifest typing
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    balance REAL DEFAULT 0.0,
    metadata BLOB,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- STRICT tables enforce rigid type checking
CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,
    account_name TEXT NOT NULL,
    balance REAL NOT NULL
) STRICT;

-- Generated columns (virtual and stored)
CREATE TABLE products (
    price REAL,
    tax_rate REAL,
    total_price REAL GENERATED ALWAYS AS (price * (1 + tax_rate)) VIRTUAL,
    discounted REAL GENERATED ALWAYS AS (price * 0.9) STORED
);

-- WITHOUT ROWID optimization for smaller tables
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

-- Indexes on expressions
CREATE INDEX idx_users_lower_email ON users(lower(email));
CREATE INDEX idx_products_total ON products(price * quantity);

-- Partial indexes for selective indexing
CREATE INDEX idx_active_users ON users(username) WHERE active = 1;

-- Row value comparisons
SELECT * FROM users WHERE (age, salary) > (25, 50000);
```

--------------------------------

### SQLite Shell Commands for Configuration and Inspection

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Showcases various dot commands within the SQLite shell for configuring output, inspecting database schemas, managing imports/exports, and optimizing the database. These commands streamline database administration tasks.

```sql
-- Display settings
.show
```

```sql
-- Set output mode
.mode column
.mode csv
.mode json
.mode table
.mode box
.mode markdown
```

```sql
-- Headers and formatting
.headers on
.width 10 20 30
.separator '|'
```

```sql
-- Database schema inspection
.tables
.tables 'user%'
.schema users
.schema
.indexes users
.fullschema
```

```sql
-- Import/export data
.mode csv
.import data.csv users
.headers on
.output users.csv
SELECT * FROM users;
.output stdout
```

```sql
-- Backup and restore
.backup mydb_backup.db
.restore mydb_backup.db
```

```sql
-- Execute SQL from file
.read schema.sql
```

```sql
-- Timing queries
.timer on
SELECT COUNT(*) FROM users;
```

```sql
-- Explain query plan
.eqp on
SELECT * FROM users WHERE age > 25;
```

```sql
-- Save output to file
.once output.txt
SELECT * FROM users;
```

```sql
-- Database integrity check
PRAGMA integrity_check;
PRAGMA quick_check;
```

```sql
-- Optimize database
PRAGMA optimize;
VACUUM;
```

### Features > Indexes On Expressions

Source: https://www.sqlite.org/docs.html

SQLite supports indexes on expressions, allowing for more flexible indexing strategies beyond simple column-based indexing.

--------------------------------

### Features > Partial Indexes

Source: https://www.sqlite.org/docs.html

A partial index covers only a subset of rows in a table, providing a way to optimize queries that target specific data ranges or conditions.

## Topic: wal mode

### Configure SQLite Write-Ahead Logging (WAL) Mode

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

This section shows how to enable and configure Write-Ahead Logging (WAL) mode in SQLite using SQL commands and C code. WAL mode improves concurrency by allowing readers and writers to operate simultaneously. It includes examples for enabling WAL, setting the autocheckpoint interval, performing manual checkpoints, and disabling WAL.

```sql
-- Enable WAL mode
PRAGMA journal_mode = WAL;

-- Configure WAL parameters
PRAGMA wal_autocheckpoint = 1000;  -- Checkpoint after 1000 pages
PRAGMA wal_checkpoint(TRUNCATE);   -- Truncate WAL file

-- Check WAL status
PRAGMA journal_mode;

-- Disable WAL (switch back to DELETE mode)
PRAGMA journal_mode = DELETE;

```

```c
#include <sqlite3.h>

int configure_wal(sqlite3 *db) {
    char *err_msg = NULL;
    int rc;

    // Enable WAL mode
    rc = sqlite3_exec(db, "PRAGMA journal_mode=WAL", NULL, NULL, &err_msg);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Cannot enable WAL: %s\n", err_msg);
        sqlite3_free(err_msg);
        return rc;
    }

    // Set autocheckpoint interval
    rc = sqlite3_wal_autocheckpoint(db, 1000);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Cannot set autocheckpoint: %s\n", sqlite3_errmsg(db));
        return rc;
    }

    // Manual checkpoint
    int nLog, nCkpt;
    rc = sqlite3_wal_checkpoint_v2(
        db,
        NULL,                          // Database name (NULL = all)
        SQLITE_CHECKPOINT_FULL,        // Checkpoint mode
        &nLog,                         // Output: WAL size
        &nCkpt                         // Output: Checkpointed frames
    );

    printf("WAL checkpoint: %d frames in WAL, %d checkpointed\n", nLog, nCkpt);

    return rc;
}

```

--------------------------------

### SQLite Pragma Commands for Configuration and Control

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Illustrates the use of PRAGMA commands to control SQLite's behavior, performance, and database settings. Examples include enabling Write-Ahead Logging (WAL), configuring cache size, managing foreign key constraints, querying database info, and performing optimization and integrity checks.

```sql
-- Enable Write-Ahead Logging for better concurrency
PRAGMA journal_mode = WAL;

-- Configure cache size (negative = KB, positive = pages)
PRAGMA cache_size = -64000;  -- 64MB cache

-- Enable foreign key constraints
PRAGMA foreign_keys = ON;

-- Query database information
PRAGMA database_list;
PRAGMA table_info(users);
PRAGMA index_list(users);

-- Memory-mapped I/O configuration
PRAGMA mmap_size = 268435456;  -- 256MB

-- Optimize database file
PRAGMA optimize;
PRAGMA vacuum;
PRAGMA incremental_vacuum(100);

-- Security and integrity checks
PRAGMA integrity_check;
PRAGMA quick_check;
PRAGMA secure_delete = ON;

-- Query performance analysis
PRAGMA query_only = ON;
EXPLAIN QUERY PLAN SELECT * FROM users WHERE email LIKE '%@example.com';
```

--------------------------------

### SQLite Shell Commands for Configuration and Inspection

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Showcases various dot commands within the SQLite shell for configuring output, inspecting database schemas, managing imports/exports, and optimizing the database. These commands streamline database administration tasks.

```sql
-- Display settings
.show
```

```sql
-- Set output mode
.mode column
.mode csv
.mode json
.mode table
.mode box
.mode markdown
```

```sql
-- Headers and formatting
.headers on
.width 10 20 30
.separator '|'
```

```sql
-- Database schema inspection
.tables
.tables 'user%'
.schema users
.schema
.indexes users
.fullschema
```

```sql
-- Import/export data
.mode csv
.import data.csv users
.headers on
.output users.csv
SELECT * FROM users;
.output stdout
```

```sql
-- Backup and restore
.backup mydb_backup.db
.restore mydb_backup.db
```

```sql
-- Execute SQL from file
.read schema.sql
```

```sql
-- Timing queries
.timer on
SELECT COUNT(*) FROM users;
```

```sql
-- Explain query plan
.eqp on
SELECT * FROM users WHERE age > 25;
```

```sql
-- Save output to file
.once output.txt
SELECT * FROM users;
```

```sql
-- Database integrity check
PRAGMA integrity_check;
PRAGMA quick_check;
```

```sql
-- Optimize database
PRAGMA optimize;
VACUUM;
```

### Features > Write-Ahead Log (WAL) Mode

Source: https://www.sqlite.org/docs.html

Write-Ahead Log (WAL) mode provides improved concurrency and performance compared to traditional rollback transactions.

--------------------------------

### Obsolete Documents → Asynchronous IO Mode

Source: https://www.sqlite.org/docs.html

This page describes the asynchronous I/O extension that was developed alongside SQLite. Using this extension could make SQLite appear more responsive by offloading database writes to a background thread. However, this extension is now deprecated and WAL mode is recommended as a replacement.

## Topic: data types

### SQLite SQL Syntax and Data Type Examples

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates SQLite's SQL dialect, including table creation with manifest typing, STRICT tables, generated columns, WITHOUT ROWID optimization, expression indexes, partial indexes, and row value comparisons. It showcases how SQLite associates types with values rather than columns.

```sql
-- Creating tables with manifest typing
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    balance REAL DEFAULT 0.0,
    metadata BLOB,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- STRICT tables enforce rigid type checking
CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,
    account_name TEXT NOT NULL,
    balance REAL NOT NULL
) STRICT;

-- Generated columns (virtual and stored)
CREATE TABLE products (
    price REAL,
    tax_rate REAL,
    total_price REAL GENERATED ALWAYS AS (price * (1 + tax_rate)) VIRTUAL,
    discounted REAL GENERATED ALWAYS AS (price * 0.9) STORED
);

-- WITHOUT ROWID optimization for smaller tables
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

-- Indexes on expressions
CREATE INDEX idx_users_lower_email ON users(lower(email));
CREATE INDEX idx_products_total ON products(price * quantity);

-- Partial indexes for selective indexing
CREATE INDEX idx_active_users ON users(username) WHERE active = 1;

-- Row value comparisons
SELECT * FROM users WHERE (age, salary) > (25, 50000);
```

--------------------------------

### SQLite Core SQL Functions for Data Manipulation

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates various built-in scalar functions in SQLite for string manipulation, mathematical operations, type checking, and conditional logic. Includes examples for length, substr, replace, typeof, cast, CASE statements, and NULL handling.

```sql
-- String functions
SELECT
    length('Hello World'),              -- 11
    substr('Hello World', 7, 5),        -- 'World'
    replace('Hello World', 'World', 'SQLite'),  -- 'Hello SQLite'
    upper('hello'), lower('WORLD'),     -- 'HELLO', 'world'
    trim('  spaces  '),                 -- 'spaces'
    printf('User %d: %s', 42, 'Alice'); -- 'User 42: Alice'

-- Math functions
SELECT
    abs(-42),                   -- 42
    round(3.14159, 2),         -- 3.14
    random(),                  -- Random integer
    hex(randomblob(16)),       -- 32-char hex string
    pow(2, 10),                -- 1024.0
    sqrt(144);                 -- 12.0

-- Type checking and conversion
SELECT
    typeof(42),                         -- 'integer'
    typeof(3.14),                       -- 'real'
    typeof('text'),                     -- 'text'
    typeof(NULL),                       -- 'null'
    cast('123' AS INTEGER),             -- 123
    quote('It''s quoted');              -- '''It''s quoted'''

-- Conditional logic
SELECT
    username,
    CASE
        WHEN balance > 1000 THEN 'Premium'
        WHEN balance > 100 THEN 'Standard'
        ELSE 'Basic'
    END AS tier,
    ifnull(email, 'no-email@example.com') AS contact,
    nullif(balance, 0) AS non_zero_balance
FROM users;
```

### SQL Language Documentation > DataTypes

Source: https://www.sqlite.org/docs.html

SQLite utilizes manifest typing, a system where the data type is associated with the individual value rather than the column where it is stored. This approach provides flexibility in how data is handled within the database.

--------------------------------

### Advocacy > Flexible Typing Is A Feature

Source: https://www.sqlite.org/docs.html

SQLite features flexible typing, allowing developers to store content in any format regardless of the column's declared datatype, which is intended as a design feature.

--------------------------------

### SQL Language Documentation > STRICT Tables

Source: https://www.sqlite.org/docs.html

STRICT tables provide a mechanism for rigid type enforcement within SQLite. This feature allows SQLite to more closely mimic the behavior and data integrity constraints found in other traditional SQL database engines.

## Topic: json functions

### SQLite JSON Table Functions and Queries

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Explains how to use JSON table functions like json_each() and json_tree() to query JSON data as relational tables. It also provides examples of inserting JSON into tables and performing queries and aggregations on JSON data.

```sql
-- json_each() to expand JSON arrays
SELECT key, value, type
FROM json_each('[1, "text", null, {"a":1}]');

-- json_tree() for recursive traversal
SELECT key, value, type, path, fullkey
FROM json_tree('{"name":"Alice","address":{"city":"NYC","zip":"10001"}}');

-- Practical JSON queries
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    data TEXT  -- JSON column
);

INSERT INTO events (data) VALUES
    ('{"user":"alice","action":"login","timestamp":1697123445}'),
    ('{"user":"bob","action":"purchase","amount":99.99,"timestamp":1697123500}'),
    ('{"user":"alice","action":"logout","timestamp":1697123600}');

-- Query JSON data
SELECT
    id,
    json_extract(data, '$.user') AS username,
    json_extract(data, '$.action') AS action,
    json_extract(data, '$.amount') AS amount
FROM events
WHERE json_extract(data, '$.user') = 'alice';

-- JSON aggregation
SELECT
    json_extract(data, '$.user') AS user,
    COUNT(*) AS event_count,
    json_group_array(json_extract(data, '$.action')) AS actions,
    json_group_object(id, json_extract(data, '$.action')) AS id_action_map
FROM events
GROUP BY user;

-- JSON_EACH with table joins
SELECT
    e.id,
    j.value AS tag
FROM events e, json_each(e.data, '$.tags') AS j
WHERE j.value = 'important';

```

--------------------------------

### SQLite JSON Creation and Manipulation

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates how to create, extract, and modify JSON data within SQLite using built-in SQL functions. It covers creating simple JSON objects and arrays, building complex structures, and using shorthand operators.

```sql
-- Creating JSON
SELECT json('{"name":"Alice","age":30}');
SELECT json_object('name', 'Alice', 'age', 30, 'active', 1);
SELECT json_array('apple', 'banana', 'cherry');
SELECT json_array(1, 2, 3, 4, 5);

-- Building complex JSON structures
SELECT json_object(
    'user', json_object('id', 1, 'name', 'Alice'),
    'preferences', json_object('theme', 'dark', 'language', 'en'),
    'tags', json_array('admin', 'verified')
);

-- Extracting JSON values
SELECT
    json_extract('{"name":"Alice","age":30}', '$.name') AS name,
    json_extract('{"user":{"id":1,"name":"Alice"}}', '$.user.id') AS user_id,
    json_extract('{"tags":["a","b","c"]}', '$.tags[1]') AS second_tag;

-- Shorthand operator ->
SELECT
    '{"name":"Alice","age":30}' -> '$.name' AS name,
    '{"user":{"id":1}}' -> '$.user.id' AS id,
    '{"user":{"id":1}}' ->> '$.user.id' AS id_as_text;

-- JSON modification
SELECT json_insert('{"a":1}', '$.b', 2);              -- {"a":1,"b":2}
SELECT json_replace('{"a":1,"b":2}', '$.a', 99);      -- {"a":99,"b":2}
SELECT json_set('{"a":1}', '$.b', 2, '$.a', 99);      -- {"a":99,"b":2}
SELECT json_remove('{"a":1,"b":2}', '$.b');           -- {"a":1}

-- JSON array operations
SELECT json_array_length('[1,2,3,4,5]');              -- 5
SELECT json_array_length('{"tags":["a","b"]}', '$.tags');  -- 2

```

--------------------------------

### SQLite CSV and Data Import/Export

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Provides bash scripts for importing data from CSV files into SQLite tables and exporting data from tables to CSV and JSON formats. Includes handling of headers and skipping rows.

```bash
# Create table for CSV import
sqlite3 mydb.db <<EOF
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    age INTEGER
);
EOF
```

```bash
# Import CSV with headers
sqlite3 mydb.db <<EOF
.mode csv
.import --csv users.csv users
EOF
```

```bash
# Import without headers (skip first line)
sqlite3 mydb.db <<EOF
.mode csv
.import --csv --skip 1 users.csv users
EOF
```

```bash
# Export to CSV
sqlite3 mydb.db <<EOF
.headers on
.mode csv
.output users_export.csv
SELECT * FROM users;
.output stdout
EOF
```

```bash
# Export to JSON
sqlite3 mydb.db <<EOF
.mode json
.output users.json
SELECT * FROM users;
.output stdout
EOF
```

```bash
# Bulk operations with transaction
sqlite3 mydb.db <<EOF
BEGIN TRANSACTION;
.mode csv
.import large_data.csv staging_table
INSERT INTO users SELECT * FROM staging_table WHERE age >= 18;
DROP TABLE staging_table;
COMMIT;
EOF
```

--------------------------------

### SQLite Shell Commands for Configuration and Inspection

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Showcases various dot commands within the SQLite shell for configuring output, inspecting database schemas, managing imports/exports, and optimizing the database. These commands streamline database administration tasks.

```sql
-- Display settings
.show
```

```sql
-- Set output mode
.mode column
.mode csv
.mode json
.mode table
.mode box
.mode markdown
```

```sql
-- Headers and formatting
.headers on
.width 10 20 30
.separator '|'
```

```sql
-- Database schema inspection
.tables
.tables 'user%'
.schema users
.schema
.indexes users
.fullschema
```

```sql
-- Import/export data
.mode csv
.import data.csv users
.headers on
.output users.csv
SELECT * FROM users;
.output stdout
```

```sql
-- Backup and restore
.backup mydb_backup.db
.restore mydb_backup.db
```

```sql
-- Execute SQL from file
.read schema.sql
```

```sql
-- Timing queries
.timer on
SELECT COUNT(*) FROM users;
```

```sql
-- Explain query plan
.eqp on
SELECT * FROM users WHERE age > 25;
```

```sql
-- Save output to file
.once output.txt
SELECT * FROM users;
```

```sql
-- Database integrity check
PRAGMA integrity_check;
PRAGMA quick_check;
```

```sql
-- Optimize database
PRAGMA optimize;
VACUUM;
```

### SQLite Documentation > SQL Syntax > JSON functions

Source: https://www.sqlite.org/docs.html

Learn about SQLite's JSON functions, which enable you to work with JSON data directly within your database. These functions allow for parsing, querying, and manipulating JSON values stored in text columns.

## Topic: triggers

### SQLite FTS5 Virtual Table Creation

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Details the creation of FTS5 virtual tables for efficient full-text searching in SQLite. It includes examples for basic FTS5 table creation and setting up FTS5 with a content table and synchronization triggers.

```sql
-- Create FTS5 table
CREATE VIRTUAL TABLE documents USING fts5(
    title,
    content,
    author,
    tokenize = 'porter ascii'
);

-- Create FTS5 with content table
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    title TEXT,
    body TEXT,
    published INTEGER
);

CREATE VIRTUAL TABLE articles_fts USING fts5(
    title,
    body,
    content='articles',
    content_rowid='id'
);

-- Triggers to keep FTS index synchronized
CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
    INSERT INTO articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- Insert data
INSERT INTO documents (title, content, author) VALUES
    ('SQLite Tutorial', 'Learn how to use SQLite database effectively', 'John Doe'),
    ('Advanced SQL Queries', 'Master complex SQL queries and optimization', 'Jane Smith'),
    ('Database Design', 'Best practices for database schema design', 'John Doe');

```

--------------------------------

### Create and Query R-Tree Spatial Indexes in SQLite

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates the creation and usage of R-Tree virtual tables in SQLite for efficient multi-dimensional range queries. Includes examples for inserting points and rectangles, performing range searches, and finding overlapping shapes.

```sql
-- Create R-Tree index for 2D points
CREATE VIRTUAL TABLE locations_idx USING rtree(
    id,              -- Integer primary key
    min_x, max_x,    -- X-axis bounds
    min_y, max_y     -- Y-axis bounds
);
```

```sql
-- Create R-Tree for rectangles
CREATE VIRTUAL TABLE buildings USING rtree(
    building_id,
    min_x, max_x,
    min_y, max_y
);
```

```sql
-- Insert point data (for points, min equals max)
INSERT INTO locations_idx VALUES (1, -73.9857, -73.9857, 40.7484, 40.7484);  -- Empire State Building
INSERT INTO locations_idx VALUES (2, -73.9851, -73.9851, 40.7580, 40.7580);  -- Times Square
```

```sql
-- Insert rectangle data
INSERT INTO buildings VALUES (1, 10.0, 20.0, 10.0, 20.0);
INSERT INTO buildings VALUES (2, 15.0, 25.0, 15.0, 25.0);
```

```sql
-- Range query: find points within bounds
SELECT id FROM locations_idx
WHERE min_x >= -74.0 AND max_x <= -73.9
  AND min_y >= 40.7 AND max_y <= 40.8;
```

```sql
-- Find overlapping rectangles
SELECT building_id FROM buildings
WHERE max_x >= 12.0 AND min_x <= 18.0
  AND max_y >= 12.0 AND min_y <= 18.0;
```

```sql
-- Find buildings containing a point
SELECT building_id FROM buildings
WHERE min_x <= 17.5 AND max_x >= 17.5
  AND min_y <= 17.5 AND max_y >= 17.5;
```

```sql
-- Nearest neighbor search (SQLite 3.24.0+)
SELECT id,
       (max_x - (-73.9857)) * (max_x - (-73.9857)) +
       (max_y - 40.7484) * (max_y - 40.7484) AS distance_squared
FROM locations_idx
WHERE min_x >= -74.1 AND max_x <= -73.8
  AND min_y >= 40.6 AND max_y <= 40.9
ORDER BY distance_squared
LIMIT 10;
```

--------------------------------

### SQL: Loading SQLite Extensions and Using Virtual Tables

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Demonstrates how to load external SQLite extensions and use virtual tables, such as the CSV virtual table. It shows the SQL commands to load extensions and create virtual tables, and how to query them.

```sql
-- Enable extension loading
.load './spellfix1'
.load './csv'

-- Use CSV virtual table
CREATE VIRTUAL TABLE temp.csvtable USING csv(
    filename='data.csv',
    header=yes
);

SELECT * FROM csvtable WHERE column1 = 'value';

-- Load custom extension
SELECT load_extension('./myextension.so');

-- Use custom functions from extension
SELECT my_custom_function(column) FROM table;

```

--------------------------------

### C: SQLite Custom Virtual Table Implementation

Source: https://context7.com/context7/www_sqlite_org-docs.html/llms.txt

Implements a basic custom virtual table in C for SQLite. This involves defining structures for the virtual table and its cursor, and implementing connection, disconnection, opening, and closing methods. It uses `sqlite3_declare_vtab` to define the table schema and `sqlite3_create_module` to register the module.

```c
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1

// Simple virtual table implementation
typedef struct simple_vtab {
    sqlite3_vtab base;
    // Custom fields
} simple_vtab;

typedef struct simple_cursor {
    sqlite3_vtab_cursor base;
    int row_id;
} simple_cursor;

static int simpleConnect(
    sqlite3 *db, 
    void *pAux,
    int argc, const char *const*argv,
    sqlite3_vtab **ppVtab,
    char **pzErr
) {
    simple_vtab *pNew;
    int rc;

    rc = sqlite3_declare_vtab(db,
        "CREATE TABLE x(id INTEGER, name TEXT, value REAL)"
    );

    if (rc == SQLITE_OK) {
        pNew = sqlite3_malloc(sizeof(*pNew));
        *ppVtab = (sqlite3_vtab*)pNew;
        if (pNew == 0) return SQLITE_NOMEM;
        memset(pNew, 0, sizeof(*pNew));
    }

    return rc;
}

static int simpleDisconnect(sqlite3_vtab *pVtab) {
    simple_vtab *p = (simple_vtab*)pVtab;
    sqlite3_free(p);
    return SQLITE_OK;
}

static int simpleOpen(sqlite3_vtab *p, sqlite3_vtab_cursor **ppCursor) {
    simple_cursor *pCur;
    pCur = sqlite3_malloc(sizeof(*pCur));
    if (pCur == 0) return SQLITE_NOMEM;
    memset(pCur, 0, sizeof(*pCur));
    *ppCursor = &pCur->base;
    return SQLITE_OK;
}

static int simpleClose(sqlite3_vtab_cursor *cur) {
    simple_cursor *pCur = (simple_cursor*)cur;
    sqlite3_free(pCur);
    return SQLITE_OK;
}

static sqlite3_module simpleModule = {
    0,                    // iVersion
    simpleConnect,        // xCreate
    simpleConnect,        // xConnect
    NULL,                 // xBestIndex
    simpleDisconnect,     // xDisconnect
    simpleDisconnect,     // xDestroy
    simpleOpen,           // xOpen
    simpleClose,          // xClose
    NULL,                 // xFilter
    NULL,                 // xNext
    NULL,                 // xEof
    NULL,                 // xColumn
    NULL,                 // xRowid
    NULL,                 // xUpdate
    NULL,                 // xBegin
    NULL,                 // xSync
    NULL,                 // xCommit
    NULL,                 // xRollback
    NULL,                 // xFindFunction
    NULL,                 // xRename
};

#ifdef _WIN32
__declspec(dllexport)
#endif
int sqlite3_simple_init(
    sqlite3 *db,
    char **pzErrMsg,
    const sqlite3_api_routines *pApi
) {
    SQLITE_EXTENSION_INIT2(pApi);
    return sqlite3_create_module(db, "simple", &simpleModule, 0);
}

```

### Extensions > Sessions

Source: https://www.sqlite.org/docs.html

The Sessions extension enables the capture of database changes into a compact file. These files can be used to revert changes to implement undo functionality or to synchronize data between similar databases.