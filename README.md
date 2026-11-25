# 最小备份链工具

完整备份镜像 + 只追加（append-only）变更段。支持：

- 创建带 SHA-256 校验的完整备份；
- 持续追加 `upsert` / `delete` 行变更，LSN 全局递增；
- 滚动多个变更段；
- 恢复到**秒级时间点**并返回恢复后的**真实数据行**；
- 删除段前计算哪些秒级恢复点、连续区间和保留策略依赖它；
- 恢复时检测**缺失、损坏（校验失败）、LSN 缺口、段重叠/重复 LSN**，立即停止并返回“最后可信点”及其已验证行；
- 零第三方依赖的本地时间线 Web 页面和 JSON API；
- 内置演示数据与故障注入，可直接验证损坏 / 缺失 / 重叠场景。

## 快速开始

要求 Python 3.11+。不需要安装第三方包。

### 方式一：npm 脚本

```bash
npm start
# 打开 http://127.0.0.1:3000
```

`npm start` 会在 `.demo-repo/` 自动播种一个 11 秒的演示备份链。可用 `PORT=8000 npm start` 改端口。

### 方式二：直接使用 Python

```bash
# 创建演示仓库
python3 -m backup_chain init ./demo-repo --demo

# 启动时间线页面
python3 -m backup_chain serve ./demo-repo --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000
```

运行测试：

```bash
npm test
# 或
python3 -m unittest discover -s tests -v
```

## 时间线页面能做什么

1. 顶部选择任意秒：
   - 拖动秒级滑块；
   - 用日期时间输入框精确到秒；
   - 点击 `‹` / `›` 逐秒移动；
   - 点击彩色健康区间跳到该区间。
2. 主区域展示该恢复点实际重放出的行，而不是只显示“成功/完成”。
3. 如果链损坏、缺失或重叠：
   - 红色横幅显示错误码和原因；
   - 标明**最后可信秒 / LSN / 完整备份或变更记录**；
   - 表格展示该可信前缀对应的实际行；
   - 可一键跳到最后可信秒。
4. 右侧备份链显示完整备份、变更段、LSN/时间范围、校验状态以及当前恢复使用的链路径。
5. 删除依赖面板：
   - 点击任意段；
   - 查看依赖它的全部恢复秒、连续区间和精确秒列表；
   - 设置 `retain_from` 后区分“保留期内仍阻塞”和“保留期外将放弃”的恢复点；
   - 可按保留策略删除或预览/执行 GC；
   - 强制删除需要显式勾选确认。
6. “写入与故障演练”面板可以追加行变更、行删除、完整备份、滚动段，以及注入：
   - 校验损坏；
   - 段文件缺失；
   - 段 LSN 重叠；
   - 重置演示仓库。

## 推荐演示路径

演示仓库以 `T` 为基准，包含：

- `T+00`：`full-000001`，3 个初始账户；
- `T+01..T+03`：`seg-000001`，LSN 1–3；
- `T+04..T+06`：`seg-000002`，LSN 4–6，包含删除 `A-1003`；
- `T+07`：`full-000002`，自包含完整镜像，之后恢复不再依赖前两个旧段；
- `T+08..T+10`：`seg-000003`，LSN 7–9。

可以这样验证：

1. 选择 `T+01`、`T+03`、`T+04`、`T+10`，观察同一行的版本变化和删除效果。
2. 选择 `seg-000001`，依赖计算显示 `T+01..T+06` 共 6 个恢复秒。
3. 点击“设为第 2 个完整备份”，保留起点变为 `T+07`；此时 `seg-000001` 没有保留期内依赖，可以删除。
4. 删除后：
   - `T+00` 仍可由 `full-000001` 恢复；
   - `T+02` 会停止并标记最后可信点 `T+00`；
   - `T+07` 之后由 `full-000002` 恢复，不受旧段删除影响。
5. 重置演示后，选择 `seg-000002`，点击“制造重叠”（用 `seg-000001` 覆盖它），再跳到 `T+04`：恢复会报 `OVERLAPPING_SEGMENT`，停在 LSN 3 / `T+03`。
6. 选择 `seg-000002` 后点击“移走所选段（缺失）”，跳到 `T+05`：会报 `MISSING_SEGMENT`，最后可信点为 LSN 3 / `T+03`。
7. 对 `seg-000001` 点击“损坏所选段”，跳到 `T+06`：会报 `CHECKSUM_MISMATCH`，不会带着损坏继续重放 `seg-000002`。

## CLI

```bash
# 初始化 / 播种
python3 -m backup_chain init /path/to/repo
python3 -m backup_chain init /path/to/demo --demo

# 写入变更
python3 -m backup_chain put /path/to/repo A-1001 \
  --json '{"name":"林一","plan":"pro","balance":120}'
python3 -m backup_chain delete-row /path/to/repo A-1003
python3 -m backup_chain roll /path/to/repo
python3 -m backup_chain full /path/to/repo

# 时间线与恢复
python3 -m backup_chain timeline /path/to/repo
python3 -m backup_chain restore /path/to/repo --at 1764000003
python3 -m backup_chain restore /path/to/repo --at 2026-11-25T12:00:00Z --json

# 删除依赖规划
python3 -m backup_chain plan-deletion /path/to/repo seg-000001
python3 -m backup_chain plan-deletion /path/to/repo 1 --retain-from 1764000007

# 预演 / 执行保留策略 GC
python3 -m backup_chain prune /path/to/repo --retain-from 1764000007
python3 -m backup_chain prune /path/to/repo --retain-from 1764000007 --apply

# 显式删除：默认 dry-run；有依赖时必须给保留起点或强制确认
python3 -m backup_chain delete-segment /path/to/repo 1
python3 -m backup_chain delete-segment /path/to/repo 1 \
  --retain-from 1764000007 --apply
python3 -m backup_chain delete-segment /path/to/repo 1 \
  --force --acknowledge-dependent-points --apply

# 故障演练
python3 -m backup_chain corrupt /path/to/repo --segment 1
python3 -m backup_chain hide-segment /path/to/repo 2
python3 -m backup_chain unhide-segment /path/to/repo 2
python3 -m backup_chain make-overlap /path/to/repo --target 2 --source 1
```

恢复命令在请求点不可信时返回退出码 `4`，并在 JSON 中保留最后可信点和已验证行；依赖删除被阻止时返回退出码 `3`。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/timeline` | 秒级边界、连续区间、完整备份、段健康状态 |
| GET | `/api/restore?at=<epoch-or-iso>` | 恢复到指定秒；返回实际行、使用的链、错误和最后可信点 |
| GET | `/api/deletion-plan?segment=1[&retain_from=ts]` | 计算段依赖的恢复点 |
| GET | `/api/prune-plan?retain_from=ts` | 保留策略 GC 预演 |
| POST | `/api/changes` | 追加 `{op,id,row,ts?}` 变更 |
| POST | `/api/fulls` | 创建完整备份 |
| POST | `/api/roll` | 滚动新段 |
| POST | `/api/delete-segment` | 删除段，支持 `retain_from` / `force` |
| POST | `/api/prune` | 预演或执行 GC |
| POST | `/api/faults/corrupt` | 注入校验损坏 |
| POST | `/api/faults/hide` | 移走段文件，制造缺失 |
| POST | `/api/faults/unhide` | 恢复移走的段 |
| POST | `/api/faults/overlap` | 用源段覆盖目标段，制造 LSN 重叠 |
| POST | `/api/demo/reset` | 仅对带 `.backup-chain-demo` 标记的仓库重置 |

## 存储模型

```text
repo/
  manifest.json
  .lock
  fulls/
    full-000001.json
    full-000002.json
  segments/
    seg-000001.jsonl
    seg-000002.jsonl
    seg-000003.jsonl
```

- `manifest.json` 记录下一个 LSN/段号、活动段、完整备份起点段和删除记录。
- 每个完整备份是自包含行镜像，载荷带 SHA-256。
- 每个变更行是一条 JSONL 记录：

```json
{
  "lsn": 4,
  "ts": 1764000004,
  "op": "delete",
  "id": "A-1003",
  "row": null,
  "sha256": "..."
}
```

- 每条变更记录独立校验，因此损坏行之前的前缀仍可被识别为最后可信前缀。
- 创建新完整备份时，如果活动段非空，会自动密封并滚动到新段；新完整备份记录 `start_segment_seq`，之后恢复可从新镜像开始。

## 恢复与删除语义

恢复 `at=T` 时：

1. 选择 `ts <= T` 的最新校验通过完整备份。
2. 从该完整备份的 `lsn + 1` 开始，按段顺序重放。
3. 只应用 `ts <= T` 的记录。
4. 校验：
   - 段文件是否存在；
   - 每条记录的 SHA-256；
   - LSN 是否严格等于期望值；
   - 是否出现重复/回退 LSN（重叠）；
   - 是否出现 LSN 跳跃（缺失段或缺失记录）。
5. 遇到第一个影响目标点的故障立即停止，不继续猜测后续内容。
6. 返回故障前已经验证的行，以及最后可信的完整备份或变更记录。

删除段 `S` 前，依赖计划按链的结构计算：校验通过的完整备份把时间线划分为若干镜像时代，在某个完整备份时代内，从该完整备份的 `start_segment_seq` 开始、且已经进入该段首个时间戳的秒级恢复点，都依赖 `S`。该算法不依赖“当前这次恢复成功重放了多远”，因此即使前序段已损坏、缺失或被删除，后续段的结构依赖仍会被列出。时间线还会用段的首/末时间戳补边界，并检查区间两端的恢复签名；若区间内部发生健康状态变化，会自动二分拆开，避免把左侧一个正常秒误画成整段绿色。结果同时给出：

- 连续依赖区间；
- 总依赖秒数；
- 小规模时间线下的精确秒列表；
- 设置 `retain_from` 后，保留期内的阻塞依赖和保留期外的过期依赖；
- 经扫描确认无记录的空滚动段可安全删除，恢复时会跳过其“已删除且已验证为空”的描述符；
- 活动追加段即使带 `force` 也不会删除；清单外孤儿段会作为未注册文件审计删除，而不是产生 500。

`POST /api/fulls` 只允许第一个完整备份通过 `rows` 显式给初始镜像；后续完整备份必须从可信链派生，传入 `rows` 会返回 `400`。

## 目录与技术栈

- `backup_chain/core.py`：仓库格式、校验、写入、恢复和故障注入。
- `backup_chain/planner.py`：时间线、依赖计算、删除和保留 GC。
- `backup_chain/server.py`：标准库 `http.server` JSON API/静态服务器。
- `backup_chain/web/`：无构建步骤的原生 HTML/CSS/JavaScript 时间线页面。
- `tests/test_backup_chain.py`：恢复、依赖删除、损坏、缺失、重叠和 HTTP 测试。

本实现刻意保持“最小”：它是本地、单仓库、标准库实现，适合作为可运行的备份链原型和交互验证工具，不替代生产级备份系统所需的加密、远程复制、并发多写入者和完整保留合规策略。
