---
name: code-review
description: 进行全面的代码审查，涵盖安全、性能与可维护性分析。适用于用户要求审查代码、查找缺陷或审计代码库时。
---

# 代码审查技能

你具备进行完整代码审查的能力。请按以下结构化方式执行：

## 审查清单

### 1. 安全（关键）

检查：
- [ ] **注入类漏洞**：SQL、命令、XSS、模板注入
- [ ] **认证问题**：硬编码凭据、弱认证
- [ ] **授权缺陷**：缺少访问控制、IDOR
- [ ] **数据泄露**：日志/错误信息中的敏感数据
- [ ] **加密**：弱算法、不当密钥管理
- [ ] **依赖**：已知漏洞（用 `npm audit`、`pip-audit` 检查）

```bash
# 快速安全扫描
npm audit                    # Node.js
pip-audit                    # Python
cargo audit                  # Rust
grep -r "password\|secret\|api_key" --include="*.py" --include="*.js"
```

### 2. 正确性

检查：
- [ ] **逻辑错误**：差一、空值处理、边界情况
- [ ] **竞态条件**：并发访问缺少同步
- [ ] **资源泄漏**：未关闭的文件、连接、内存
- [ ] **错误处理**：吞掉异常、缺少错误分支
- [ ] **类型安全**：隐式转换、any 类型

### 3. 性能

检查：
- [ ] **N+1 查询**：循环内调用数据库
- [ ] **内存问题**：大分配、持有多余引用
- [ ] **阻塞操作**：在异步代码里做同步 I/O
- [ ] **低效算法**：能用 O(n) 却用 O(n²)
- [ ] **缺少缓存**：重复昂贵计算

### 4. 可维护性

检查：
- [ ] **命名**：清晰、一致、有描述性
- [ ] **复杂度**：函数超过 50 行、嵌套超过 3 层
- [ ] **重复**：复制粘贴的代码块
- [ ] **死代码**：未用导入、不可达分支
- [ ] **注释**：过时、冗余或该写没写

### 5. 测试

检查：
- [ ] **覆盖**：关键路径有测试
- [ ] **边界**：空值、空集、边界值
- [ ] **Mock**：外部依赖已隔离
- [ ] **断言**：有意义、具体

## 审查输出格式

```markdown
## 代码审查：[文件/组件名]

### 概要
[1～2 句概述]

### 关键问题
1. **[问题]**（第 X 行）：[描述]
   - 影响：[可能后果]
   - 修复：[建议方案]

### 改进建议
1. **[建议]**（第 X 行）：[描述]

### 优点
- [做得好的地方]

### 结论
[ ] 可以合并
[ ] 需小幅修改
[ ] 需大幅修改
```

## 常见需标注模式

### Python
```python
# 差：SQL 注入
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
# 好：
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

# 差：命令注入
os.system(f"ls {user_input}")
# 好：
subprocess.run(["ls", user_input], check=True)

# 差：可变默认参数
def append(item, lst=[]):  # 缺陷：共享可变默认值
# 好：
def append(item, lst=None):
    lst = lst or []
```

### JavaScript/TypeScript
```javascript
// 差：原型污染
Object.assign(target, userInput)
// 好：
Object.assign(target, sanitize(userInput))

// 差：使用 eval
eval(userCode)
// 好：绝不用用户输入调用 eval

// 差：回调地狱
getData(x => process(x, y => save(y, z => done(z))))
// 好：
const data = await getData();
const processed = await process(data);
await save(processed);
```

## 审查常用命令

```bash
# 查看近期变更
git diff HEAD~5 --stat
git log --oneline -10

# 查找潜在问题
grep -rn "TODO\|FIXME\|HACK\|XXX" .
grep -rn "password\|secret\|token" . --include="*.py"

# 检查复杂度（Python）
pip install radon && radon cc . -a

# 检查依赖
npm outdated  # Node
pip list --outdated  # Python
```

## 审查流程

1. **理解背景**：阅读 PR 说明、关联 issue
2. **运行代码**：尽量本地构建、测试、运行
3. **自顶向下阅读**：从主入口开始
4. **看测试**：改动有测试吗？测试通过吗？
5. **安全扫描**：跑自动化工具
6. **人工审查**：按上述清单逐项检查
7. **写反馈**：具体、给修复建议、语气友善
