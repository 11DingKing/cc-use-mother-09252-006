# 国际课程质量证据链

面向合作院校课程质量评审的纯服务端系统。不同机构反复提交课程大纲、师资、考核与企业反馈时，
系统以**内容指纹 + 版本关系**保存质量证据：评审决定固定到一组明确材料，后补文件只能生成新的
复审请求，敏感企业反馈按机构与角色最小披露。

## 架构

代码按领域模型、应用服务、持久化与接口边界组织；时间与标识通过可替换端口注入
（`ports.Clock` / `ports.IdGenerator`），测试可稳定复现状态变化。

```
service_09252_006/
├── models.py        # 持久化模型：证据版本链、评审包、评审、异议、复审、审计
├── fingerprint.py   # SHA-256 内容指纹与规范化哈希
├── ports.py         # 可替换端口：时钟、ID 生成
├── db.py            # 引擎/会话：WAL、外键、写事务 BEGIN IMMEDIATE 串行化
├── access.py        # 最小披露访问策略（机构 × 角色 × 评审窗口）
├── audit.py         # 追加式审计日志（哈希链 + 链头锚点）
├── verification.py  # 离线完整性核验
├── services/        # 应用服务：directory / evidence / packages / reviews
├── api.py           # HTTP 接口边界（FastAPI，X-Actor-Id 身份端口）
└── cli.py           # verify（离线核验）/ serve
```

### 核心语义

- **证据与版本**：内容以 SHA-256 寻址，版本经 `supersedes_id` 串链；相同内容重复提交
  返回既有版本（天然幂等），历史不可覆盖，只能追加新版本。
- **评审包**：封存（seal）时对钉住的材料清单计算清单指纹 `manifest_hash`，此后材料集合
  不可更改；结论签发时把 `manifest_hash` 与结论内容一起计入 `decision_hash`。
- **后补文件**：截止后或封存后到达的版本只生成复审请求；复审开启新 cycle 的评审包，
  `(机构, 学期, cycle)` 唯一约束保证并发复审只有一个胜者。
- **材料撤回**：草稿包移除条目；评审中的包整体失效（可从失效包开启新轮次恢复）；
  已签发结论的包保持不动并自动生成复审请求——历史始终可核验。
- **最小披露**：restricted（企业反馈）仅质量官、本机构管理员、提交者本人，以及处于
  有效评审窗口内的指派评审员可见内容；签发或撤销指派后窗口立即关闭。
- **跨时区**：全部时间戳以 UTC 存储与比较，接口按机构时区回显本地截止。
- **幂等与恢复**：所有写操作在单事务内完成并随写审计事件；支持 `Idempotency-Key`
  重放（同键不同体返回 409）；唯一约束 + CAS 状态迁移兜底并发。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export QEV_DATABASE_URL=sqlite:////var/lib/qev/qev.db   # 缺省在用户数据目录
.venv/bin/python -m service_09252_006.cli serve --host 0.0.0.0 --port 8000
```

身份通过 `X-Actor-Id` 请求头传入（可替换的内部身份端口）；系统首个用户必须是质量官，
其后由质量官创建机构与用户。变更类端点接受 `Idempotency-Key` 头。

## 离线完整性核验

不需要服务进程，直接核验数据库文件中的全部指纹与链式结构：

```bash
.venv/bin/python -m service_09252_006.cli verify --database-url sqlite:////var/lib/qev/qev.db
# 退出码 0 = 完整；1 = 发现篡改（内容指纹、版本链、包清单、结论、审计链）
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/users` `/institutions` | 用户/机构登记（首个用户为质量官引导） |
| PATCH | `/users/{id}` | 角色/机构/停用变更，立即生效 |
| POST | `/evidence/items` | 登记材料（自然键幂等） |
| POST | `/evidence/items/{id}/versions` | 接收证据内容（指纹+版本链+迟到判定） |
| POST | `/evidence/versions/{id}/withdraw` | 撤回材料（历史保留） |
| GET | `/evidence/versions/{id}/content` | 取回内容（最小披露） |
| POST | `/packages` `/packages/{id}/items` `/packages/{id}/seal` | 封装评审包 |
| POST | `/packages/{id}/assignments` / DELETE `.../{reviewer_id}` | 指派/撤销评审 |
| POST | `/packages/{id}/reviews` | 提交评审意见 |
| POST | `/packages/{id}/objections` `/objections/{id}/resolve` | 记录/处理异议 |
| POST | `/packages/{id}/decision` | 签发结论（固定到材料清单） |
| POST | `/packages/{id}/rereview` | 开启复审新轮次（并发安全） |

## 测试

```bash
python3 -m unittest discover -s tests -v     # 或 .venv/bin/python -m pytest tests -q
```

覆盖：材料撤回、权限变化（角色调整/撤销指派/停用）、跨时区截止、并发复审/封存/签发、
幂等重放、离线核验与篡改检测。

## 编译检查

```bash
python3 -m compileall -q service_09252_006 tests
```

运行数据与本地配置不写入源码目录（数据库默认位于 `$XDG_DATA_HOME/qev/`）。
