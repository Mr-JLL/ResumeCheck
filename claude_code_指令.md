# ResumeCheck 代码清理任务

## 背景
这是一个 Python Flask 项目（前程无忧简历抓取与评估系统），由 Vibe Coding 生成，存在冗余代码需要清理。

---

## 任务一：删除两个无用文件

直接删除以下两个文件，它们在整个仓库中没有任何文件 import 或调用它们：

1. `read_xlsx2.py` — 临时调试脚本
2. `scanner_main.py` — 已被 Web UI 取代的遗留命令行入口

```bash
rm read_xlsx2.py
rm scanner_main.py
```

删除后请确认其他文件中没有对它们的引用：
```bash
grep -rn "read_xlsx2\|scanner_main" --include="*.py" .
```
预期结果：无输出（无引用）。

---

## 任务二：新建 `browser_utils.py` 并合并重复函数

`app.py` 和 `node_agent.py` 中以下 4 个函数**完全相同**，需要提取到公共模块：

- `_find_edge_exe()`
- `_write_profile_prefs(profile_dir, download_dir)`
- `_snapshot_dir(directory)`
- `_wait_for_new_file(directory, before_set, timeout=15)`

**步骤：**

1. 新建 `browser_utils.py`，内容如下（从 `app.py` 直接复制这 4 个函数，保留原有注释）

2. 在 `app.py` 顶部 import 区域添加：
   ```python
   from browser_utils import _find_edge_exe, _write_profile_prefs, _snapshot_dir, _wait_for_new_file
   ```

3. 删除 `app.py` 中这 4 个函数的定义体（保留 import，不要留空函数）

4. 在 `node_agent.py` 顶部 import 区域添加：
   ```python
   from browser_utils import _find_edge_exe, _write_profile_prefs, _snapshot_dir, _wait_for_new_file
   ```

5. 删除 `node_agent.py` 中这 4 个函数的定义体

**完成后验证：**
```bash
# 确认函数只在 browser_utils.py 中定义
grep -rn "^def _find_edge_exe\|^def _write_profile_prefs\|^def _snapshot_dir\|^def _wait_for_new_file" --include="*.py" .
# 预期：只有 browser_utils.py 出现

# 确认两个文件都能正确 import
python -c "import app; print('app.py OK')"
python -c "import node_agent; print('node_agent.py OK')"
```

---

## 任务三：验证整体可用性

完成以上修改后，运行以下验证确保没有引入新问题：

```bash
# 检查所有 Python 文件语法
python -m py_compile app.py && echo "app.py syntax OK"
python -m py_compile node_agent.py && echo "node_agent.py syntax OK"
python -m py_compile browser_utils.py && echo "browser_utils.py syntax OK"

# 检查没有遗留的未定义引用
python -c "
import ast, os
for f in ['app.py', 'node_agent.py', 'browser_utils.py']:
    with open(f) as fp:
        ast.parse(fp.read())
    print(f'{f}: AST parse OK')
"
```

---

## 注意事项

- **不要修改** `_prewarm_driver()` 和 `_do_scrape()`，这两个函数在 `app.py` 和 `node_agent.py` 中虽然名字相同但逻辑有差异，需要人工判断后再处理
- **不要修改** 任何 HTML 模板和 JS 文件
- 所有修改完成后，建议用 `git diff` 检查变更范围是否符合预期
