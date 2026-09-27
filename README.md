# 文创设计权利转换

承接创意征集 → 素材贡献 → 机器辅助设计 → 人工修改 → 权属确认 → 打样验收 → 商品授权 → 结算分配的完整链路。
`domain.json` 统一贡献类型、用途范围、授权状态等词表；`python3 service.py --check` 校验词表与引擎自检。

## 设计要点

- **来源图（provenance DAG）**：每次合并/修改都生成新的设计版本节点，带权入边记录父版本（可多个）、直接素材与机器批次候选；素材占比沿父版本与批次递归传播，节点入边占比之和必须为 1。
- **按用途范围逐条授权**：许可精确到「校内展示 / 公益展览 / 商品生产 / 渠道宣传」。校园展示许可**不会**自动扩展为商业许可，商业用途必须单独契约确认。
- **未成年人保护**：未成年学生必须登记监护人；其作品的任何许可（含校内展示）都只能由监护人确认，否则权属确认不予通过。
- **撤回/争议不删记录**：只追加事件（append-only 事件账本），冻结下游设计、商品与下载授权，并按 **尚未生产 / 在制（含已入库）/ 已售出** 三类给出影响与处置策略；销售、批次、历史授权版本全部保留。
- **供应商受控下载**：供应商只能取得其订单对应商品的**可水印**文件（水印含订单/供应商/授权版本），受下载次数与到期时间双重限制；商品冻结即废止未用授权。
- **可追溯结算**：按最终确认的贡献比例（设计占比 × 素材占比）分配，金额按分取整、尾差补给最大占比方，保证分毫不差；每笔分配锁定授权版本号与权属快照哈希。
- **幂等**：所有写操作支持 `Idempotency-Key`（或 body 内 `idem`）。重复的打样回调、支付回调、驳回重提、授权版本更新都只生效一次，账目与来源图保持一致。
- **一致性自检**：`GET /consistency` 检查下载次数/期限、授权快照与来源图漂移、待结算批次与现行授权版本是否一致、分配金额是否找平。

## 运行

```bash
python3 service.py --check          # 校验配置
python3 service.py --port 8000      # 仅内存
python3 service.py --data state.json  # JSON 持久化（原子写，重启恢复幂等记录）
python3 -m unittest -v              # 16 个端到端测试
```

## 接口一览（均为 JSON）

| 阶段 | 接口 |
|---|---|
| 创意简报 | `POST /briefs` |
| 贡献者 | `POST /contributors`（未成年学生必须带 `guardian.name/contact`） |
| 素材贡献 | `POST /materials` |
| 用途授权 | `POST /materials/{id}/confirm`（`action`: 首次确认/续约确认/**监护确认**/契约确认，按 `scope` 逐条） |
| 生成批次 | `POST /batches`（素材占比之和为 1，自动产出 3 个候选） |
| 设计版本 | `POST /designs`（`material_edges`/`parent_edges`/`batch_edges` 三类带权边）；`GET /designs/{id}/provenance` |
| 权属确认 | `POST /designs/{id}/review-request`；`POST /designs/{id}/review`（缺口以 422 返回：缺授权/缺监护确认/校园许可越界商用） |
| 打样验收 | `POST /samples`；`POST /samples/{id}/rounds`（支持 `callback_id`，驳回可重提、轮次自增）；`.../verify`；`.../seal` |
| 商品 | `POST /products`；`POST /products/{id}/activate`；`POST /production-batches`（未排产/生产中/已入库/已结案）；`POST /sales` |
| 授权版本 | `POST /products/{id}/licenses`；`POST /products/{id}/licenses/update`（范围扩展/范围缩减/版本更新，旧版留痕）；`POST /products/{id}/revise`（撤回后以新版本替换恢复） |
| 供应商 | `POST /orders`；`POST /orders/{id}/grants`（水印文件+次数+期限）；`POST /grants/{id}/fetch` |
| 撤回/争议 | `GET /materials/{id}/impact`（三分影响）；`POST /materials/{id}/withdraw`（可按单条 `scope` 部分撤回）；`POST /materials/{id}/dispute`；`.../dispute/resolve` |
| 结算 | `POST /settlements`（锁授权版本与快照哈希，漂移拒绝）；`.../lock`；`.../callback`（支付回调幂等） |
| 运维 | `GET /health`、`GET /domain`、`GET /consistency`、`GET /events?entity_type=&entity_id=` |

## 典型边界行为

- 未成年人素材用「首次确认」送审商业用途 → 拒绝；补「监护确认」后通过。
- 学校组织素材只有校内展示许可，设计申请商品生产 → 判定「校园许可越界商用」；签订商业契约后放行。
- 同一素材分别用于仅校内与商业两个设计，撤回其商业许可只冻结商业设计，校内设计不受影响。
- 撤回素材：未排产批次可撤单改版、在制批次冻结、已售数量保留并追溯；商品「召回冻结」期间禁止销售、下单、放行文件与结算。
- 授权版本更新导致来源比例变化时，未锁定的旧结算批次会被一致性检查标记，新结算批次按新授权版本重算；已锁定批次保留其历史快照不动账。
