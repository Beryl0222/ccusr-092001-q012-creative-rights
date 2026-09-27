"""核心领域引擎：创意简报、素材贡献、生成批次、设计来源图、权属确认、
打样验收、商品授权、供应商受控下载与结算分配。

设计原则：
* 所有写操作追加事件账本（append-only），实体为账本的物化视图；撤回/争议
  从不删除记录，只产生新事件并冻结下游。
* 每个设计节点保存父版本与所用素材的边（provenance DAG）。
* 每个授权/确认按"用途范围"逐条授予；校园展示不会自动扩展为商业许可。
* 所有命令支持幂等键，重复回调/驳回重提/版本更新只生效一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

DOMAIN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "domain.json")

# 词表外的动作词
CONFIRM_ACTIONS = {"首次确认", "续约确认", "监护确认", "契约确认"}
COMMERCIAL_SCOPES = {"商品生产", "渠道宣传"}
SCHOOL_SCOPES = {"校内展示", "公益展览"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_dt(value: str) -> str:
    """把到期时间统一为带时区的 ISO 字符串，保证可按字符串与 utcnow 比较。"""
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    """领域规则冲突；HTTP 层映射为 4xx。"""

    def __init__(self, message: str, code: str = "domain_rule", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def _cents(amount) -> int:
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _money(cents: int) -> float:
    return round(cents / 100, 2)


def load_domain(path: str = DOMAIN_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    required = ["贡献类型", "用途范围", "授权状态"]
    for key in required:
        if not isinstance(data.get(key), list) or not data[key]:
            raise ValueError(f"domain.json 缺少词表：{key}")
    return data


class RightsEngine:
    """状态保存在内存，可镜像到 JSON 文件。"""

    def __init__(self, domain: dict | None = None, data_path: str | None = None):
        self.domain = domain or load_domain()
        self.data_path = data_path
        self.lock = threading.RLock()
        self.seq = 0
        self._store = self._empty_store()
        if data_path and os.path.exists(data_path):
            self._load()

    # ------------------------------------------------------------------ 基础设施

    @staticmethod
    def _empty_store() -> dict:
        return {
            "briefs": {},
            "contributors": {},
            "materials": {},
            "batches": {},
            "designs": {},
            "design_edges": [],
            "samples": {},
            "products": {},
            "production_batches": {},
            "sales": [],
            "orders": {},
            "download_grants": {},
            "license_versions": {},
            "licenses": {},
            "royalty_runs": {},
            "events": [],
            "idempotency": {},
        }

    def _load(self):
        with open(self.data_path, encoding="utf-8") as fh:
            self._store = json.load(fh)
        self.seq = max((e["seq"] for e in self._store["events"]), default=0)

    def _save(self):
        if not self.data_path:
            return
        tmp = f"{self.data_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._store, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.data_path)

    def _id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}_{self.seq:04d}"

    def _event(self, etype: str, entity_type: str, entity_id: str, payload: dict) -> dict:
        self.seq += 1
        evt = {
            "seq": self.seq,
            "at": utcnow(),
            "type": etype,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
        }
        self._store["events"].append(evt)
        return evt

    def _idem(self, bucket: str, key: str | None, producer):
        """幂等执行：同一 (bucket, key) 永远返回首次结果。"""
        if key is None:
            with self.lock:
                result = producer()
                self._save()
            return result
        full = f"{bucket}:{key}"
        with self.lock:
            cached = self._store["idempotency"].get(full)
            if cached is not None:
                cached = dict(cached)
                cached["idempotent_replay"] = True
                return cached
            try:
                result = producer()
            except Exception:
                self._save()
                raise
            self._store["idempotency"][full] = result
            self._save()
            return result

    def _require(self, collection: str, entity_id: str) -> dict:
        entity = self._store[collection].get(entity_id)
        if entity is None:
            raise DomainError(f"{entity_id} 不存在", code="not_found", status=404)
        return entity

    def _vocab(self, key: str, value: str):
        if value not in self.domain[key]:
            raise DomainError(f"{value} 不在词表 {key} 中：{self.domain[key]}")

    # ------------------------------------------------------------------ 简报与贡献者

    def create_brief(self, title: str, summary: str = "", idem: str | None = None) -> dict:
        def go():
            bid = self._id("brief")
            brief = {"id": bid, "title": title, "summary": summary, "at": utcnow()}
            self._store["briefs"][bid] = brief
            self._event("brief_created", "brief", bid, {"title": title})
            return brief
        return self._idem("brief", idem, go)

    def register_contributor(self, ctype: str, name: str,
                             guardian: dict | None = None, idem: str | None = None) -> dict:
        def go():
            self._vocab("贡献者类型", ctype)
            cid = self._id("contrib")
            contributor = {
                "id": cid, "type": ctype, "name": name,
                "guardian": guardian or None, "blocked": False, "at": utcnow(),
            }
            if ctype == "未成年学生":
                if not guardian or not guardian.get("name") or not guardian.get("contact"):
                    raise DomainError("未成年学生必须登记监护人姓名与联系方式")
            self._store["contributors"][cid] = contributor
            self._event("contributor_registered", "contributor", cid,
                        {"type": ctype, "name": name})
            return contributor
        return self._idem("contributor", idem, go)

    # ------------------------------------------------------------------ 素材与授权

    def contribute_material(self, brief_id: str, contributor_id: str, kind: str,
                            title: str, file_ref: str = "", note: str = "",
                            idem: str | None = None) -> dict:
        def go():
            self._require("briefs", brief_id)
            contributor = self._require("contributors", contributor_id)
            self._vocab("贡献类型", kind)
            mid = self._id("mat")
            material = {
                "id": mid, "brief_id": brief_id,
                "contributor_id": contributor_id, "kind": kind, "title": title,
                "file_ref": file_ref, "note": note,
                "status": "待确认", "grants": [], "at": utcnow(),
            }
            self._store["materials"][mid] = material
            self._event("material_contributed", "material", mid,
                        {"contributor_id": contributor_id, "kind": kind,
                         "contributor_type": contributor["type"]})
            return material
        return self._idem("material", idem, go)

    def confirm_scope(self, material_id: str, scope: str, action: str, by: str,
                      idem: str | None = None) -> dict:
        """按用途范围授予许可。每个范围独立授权，绝不自动扩展。"""
        def go():
            material = self._require("materials", material_id)
            contributor = self._require("contributors", material["contributor_id"])
            self._vocab("用途范围", scope)
            if action not in CONFIRM_ACTIONS:
                raise DomainError(f"未知确认动作 {action}")

            if contributor["type"] == "未成年学生":
                if action != "监护确认":
                    raise DomainError("未成年学生作品的任何许可都必须由监护人确认")
                if not contributor.get("guardian"):
                    raise DomainError("缺少监护人信息")
            if contributor["type"] == "学校组织" and scope in COMMERCIAL_SCOPES:
                if action != "契约确认":
                    raise DomainError("学校组织作品的商业用途必须经契约确认，校内/公益许可不自动延展")
            if contributor["type"] == "供应商打样方" and scope in COMMERCIAL_SCOPES \
                    and action not in {"契约确认", "监护确认"}:
                raise DomainError("供应商打样方的商业用途必须经契约确认")

            # 已存在同一范围的有效授权：幂等返回，不重复记账
            for grant in material["grants"]:
                if grant["scope"] == scope and grant["status"] == "有效":
                    return {"material_id": material_id, "grant": grant, "unchanged": True}

            evt = self._event("scope_granted", "material", material_id,
                              {"scope": scope, "action": action, "by": by})
            grant = {
                "scope": scope, "status": "有效", "action": action, "by": by,
                "event_id": evt["seq"], "at": evt["at"],
            }
            material["grants"].append(grant)
            if material["status"] in {"待确认", "受限"}:
                material["status"] = "有效"
            return {"material_id": material_id, "grant": grant}
        return self._idem(f"confirm:{material_id}:{scope}", idem, go)

    def _effective_grant(self, material: dict, scope: str) -> dict | None:
        for grant in material["grants"]:
            if grant["scope"] == scope and grant["status"] == "有效":
                return grant
        return None

    # ------------------------------------------------------------------ 生成批次

    def create_batch(self, brief_id: str, prompt: str, source_material_ids: list[dict],
                     model: str, operator: str, idem: str | None = None) -> dict:
        """source_material_ids: [{material_id, share}]，占比之和应为 1。"""
        def go():
            self._require("briefs", brief_id)
            total = Decimal("0")
            normalized = []
            for src in source_material_ids:
                mat = self._require("materials", src["material_id"])
                share = Decimal(str(src.get("share", 0)))
                if share < 0 or share > 1:
                    raise DomainError("素材占比必须在 0~1 之间")
                total += share
                normalized.append({"material_id": src["material_id"],
                                   "share": float(share), "kind": mat["kind"]})
            if source_material_ids and total != Decimal("1"):
                raise DomainError(f"生成批次素材占比之和必须为 1，当前 {total}")
            bid = self._id("batch")
            batch = {
                "id": bid, "brief_id": brief_id, "prompt": prompt,
                "sources": normalized, "model": model, "operator": operator,
                "status": "已生成", "items": [], "at": utcnow(),
            }
            for i in range(1, 4):
                batch["items"].append({"item_id": f"{bid}#item{i}", "label": f"候选 {i}"})
            self._store["batches"][bid] = batch
            self._event("batch_generated", "batch", bid,
                        {"prompt": prompt, "sources": normalized, "model": model})
            return batch
        return self._idem("batch", idem, go)

    # ------------------------------------------------------------------ 设计版本 DAG

    def create_design(self, brief_id: str, title: str, created_by: str,
                      material_edges: list[dict] | None = None,
                      parent_edges: list[dict] | None = None,
                      batch_edges: list[dict] | None = None,
                      idem: str | None = None) -> dict:
        """创建设计节点（每次合并/修改都是新节点）。三类带权入边，占比之和为 1：

        material_edges: [{"material_id", "share"}] 直接使用的素材
        parent_edges:   [{"design_id", "share"}]   合并的父版本（可多个，支持合并）
        batch_edges:    [{"batch_item_id", "share"}] 机器候选，占比沿批次来源传播
        """
        def go():
            self._require("briefs", brief_id)
            mat_edges = material_edges or []
            par_edges = parent_edges or []
            bat_edges = batch_edges or []

            total = Decimal("0")
            for edge in mat_edges:
                self._require("materials", edge["material_id"])
                share = Decimal(str(edge.get("share", 0)))
                if share < 0 or share > 1:
                    raise DomainError("素材占比必须在 0~1 之间")
                total += share
            for edge in par_edges:
                parent = self._require("designs", edge["design_id"])
                if parent["status"] in {"已撤回", "争议冻结"}:
                    raise DomainError(f"父版本 {edge['design_id']} 处于 {parent['status']}，不可合并")
                share = Decimal(str(edge.get("share", 0)))
                if share < 0 or share > 1:
                    raise DomainError("父版本占比必须在 0~1 之间")
                total += share
            for edge in bat_edges:
                item_id = edge["batch_item_id"]
                batch_id = item_id.split("#")[0]
                batch = self._require("batches", batch_id)
                if not any(it["item_id"] == item_id for it in batch["items"]):
                    raise DomainError(f"批次候选 {item_id} 不存在")
                share = Decimal(str(edge.get("share", 0)))
                if share < 0 or share > 1:
                    raise DomainError("批次候选占比必须在 0~1 之间")
                total += share
            if total != Decimal("1"):
                raise DomainError(f"设计入边占比之和必须为 1，当前 {total}")

            did = self._id("design")
            design = {
                "id": did, "brief_id": brief_id, "title": title,
                "created_by": created_by, "status": "编辑中",
                "material_edges": mat_edges, "parent_edges": par_edges,
                "batch_edges": bat_edges, "review": None, "at": utcnow(),
            }
            self._store["designs"][did] = design
            for edge in mat_edges:
                self._store["design_edges"].append(
                    {"design_id": did, "kind": "material",
                     "ref": edge["material_id"], "share": float(edge["share"])})
            for edge in par_edges:
                self._store["design_edges"].append(
                    {"design_id": did, "kind": "parent",
                     "ref": edge["design_id"], "share": float(edge["share"])})
            for edge in bat_edges:
                self._store["design_edges"].append(
                    {"design_id": did, "kind": "batch_item",
                     "ref": edge["batch_item_id"], "share": float(edge["share"])})
            self._event("design_created", "design", did,
                        {"title": title, "materials": mat_edges,
                         "parents": par_edges, "batch_items": bat_edges})
            return design
        return self._idem("design", idem, go)

    def provenance(self, design_id: str) -> dict:
        """汇总设计的全部素材来源（沿带权边递归父版本/批次），返回有效占比与机器来源标记。"""
        design = self._require("designs", design_id)
        weights: dict[str, Decimal] = {}
        machine = False

        def walk(did: str, mult: Decimal):
            nonlocal machine
            node = self._store["designs"][did]
            if node["batch_edges"]:
                machine = True
            for edge in node["batch_edges"]:
                batch_id = edge["batch_item_id"].split("#")[0]
                batch = self._store["batches"][batch_id]
                sub = mult * Decimal(str(edge["share"]))
                for src in batch["sources"]:
                    weights[src["material_id"]] = (
                        weights.get(src["material_id"], Decimal("0"))
                        + sub * Decimal(str(src["share"])))
            for edge in node["material_edges"]:
                mid = edge["material_id"]
                weights[mid] = (weights.get(mid, Decimal("0"))
                                + mult * Decimal(str(edge["share"])))
            for edge in node["parent_edges"]:
                walk(edge["design_id"], mult * Decimal(str(edge["share"])))

        walk(design_id, Decimal("1"))
        materials = []
        for mid, weight in sorted(weights.items()):
            mat = self._store["materials"][mid]
            materials.append({
                "material_id": mid,
                "title": mat["title"],
                "weight": float(weight.quantize(Decimal("0.0001"))),
                "contributor_id": mat["contributor_id"],
                "contributor_type": self._store["contributors"][mat["contributor_id"]]["type"],
                "status": mat["status"],
                "scopes": [g["scope"] for g in mat["grants"] if g["status"] == "有效"],
            })
        return {
            "design_id": design_id,
            "title": design["title"],
            "machine_assisted": machine,
            "materials": materials,
            "weight_sum": float(sum(weights.values()).quantize(Decimal("0.0001"))),
        }

    # ------------------------------------------------------------------ 权属确认

    def request_review(self, design_id: str, idem: str | None = None) -> dict:
        def go():
            design = self._require("designs", design_id)
            if design["status"] in {"争议冻结", "已撤回"}:
                raise DomainError(f"设计当前 {design['status']}，不能送审")
            if design["status"] == "受限冻结":
                self._event("review_resubmitted_after_freeze", "design", design_id, {})
            design["status"] = "待权属确认"
            self._event("review_requested", "design", design_id, {})
            return {"design_id": design_id, "status": design["status"]}
        return self._idem("review_req", idem, go)

    def _clearance_gaps(self, design_id: str, scopes: list[str]) -> list[dict]:
        prov = self.provenance(design_id)
        gaps = []
        if abs(prov["weight_sum"] - 1.0) > 1e-4:
            gaps.append({"type": "占比不平", "weight_sum": prov["weight_sum"]})
        for item in prov["materials"]:
            mat = self._store["materials"][item["material_id"]]
            contributor = self._store["contributors"][item["contributor_id"]]
            for scope in scopes:
                grant = self._effective_grant(mat, scope)
                if grant is None:
                    gaps.append({
                        "type": "缺少授权", "material_id": item["material_id"],
                        "scope": scope, "contributor_type": contributor["type"],
                    })
                    continue
                if contributor["type"] == "未成年学生" and grant["action"] != "监护确认":
                    gaps.append({"type": "缺监护确认", "material_id": item["material_id"],
                                 "scope": scope})
                if contributor["type"] == "学校组织" and scope in COMMERCIAL_SCOPES \
                        and grant["action"] != "契约确认":
                    gaps.append({"type": "校园许可越界商用", "material_id": item["material_id"],
                                 "scope": scope, "grant_action": grant["action"]})
        return gaps

    def decide_review(self, design_id: str, scopes: list[str], reviewer: str,
                      idem: str | None = None) -> dict:
        def go():
            design = self._require("designs", design_id)
            for scope in scopes:
                self._vocab("用途范围", scope)
            gaps = self._clearance_gaps(design_id, scopes)
            if gaps:
                design["status"] = "待权属确认"
                self._event("review_rejected", "design", design_id, {"gaps": gaps})
                raise _ClearanceFailure(gaps)
            snapshot = []
            for item in self.provenance(design_id)["materials"]:
                for scope in scopes:
                    grant = self._effective_grant(self._store["materials"][item["material_id"]],
                                                  scope)
                    snapshot.append({
                        "material_id": item["material_id"],
                        "contributor_id": item["contributor_id"],
                        "weight": item["weight"], "scope": scope,
                        "grant_event_id": grant["event_id"],
                    })
            snap_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            design["status"] = "已确认"
            design["review"] = {
                "scopes": scopes, "reviewer": reviewer, "at": utcnow(),
                "snapshot": snapshot, "snapshot_hash": snap_hash,
            }
            self._event("review_confirmed", "design", design_id,
                        {"scopes": scopes, "snapshot_hash": snap_hash})
            return {"design_id": design_id, "status": "已确认",
                    "scopes": scopes, "snapshot_hash": snap_hash}
        try:
            return self._idem("review_decide", idem, go)
        except _ClearanceFailure as exc:
            raise DomainError("权属确认未通过：" + json.dumps(exc.gaps, ensure_ascii=False),
                              code="clearance_failed") from exc

    # ------------------------------------------------------------------ 打样验收

    def create_sample(self, design_id: str, supplier_id: str, spec: str,
                      product_id: str | None = None, idem: str | None = None) -> dict:
        def go():
            self._require("designs", design_id)
            if product_id:
                product = self._require("products", product_id)
                if product["status"] == "召回冻结":
                    raise DomainError("商品处于召回冻结，不能新开打样")
            sid = self._id("sample")
            sample = {
                "id": sid, "design_id": design_id, "supplier_id": supplier_id,
                "product_id": product_id, "spec": spec,
                "status": "待打样", "rounds": [], "at": utcnow(),
            }
            self._store["samples"][sid] = sample
            self._event("sample_created", "sample", sid, {"design_id": design_id})
            return sample
        return self._idem("sample", idem, go)

    def submit_sample_round(self, sample_id: str, file_ref: str, note: str = "",
                            callback_id: str | None = None,
                            idem: str | None = None) -> dict:
        """供应商提交一版打样（回调驱动）。驳回后可重提，轮次自增；重复回调不重复记账。"""
        def go():
            sample = self._require("samples", sample_id)
            if sample["status"] not in {"待打样", "已驳回", "打样中"}:
                raise DomainError(f"样品当前 {sample['status']}，不能提交新版本")
            round_no = len(sample["rounds"]) + 1
            round_record = {
                "round": round_no, "file_ref": file_ref, "note": note,
                "callback_id": callback_id, "at": utcnow(), "verdict": None,
            }
            sample["rounds"].append(round_record)
            sample["status"] = "待验收"
            self._event("sample_submitted", "sample", sample_id,
                        {"round": round_no, "callback_id": callback_id})
            return {"sample_id": sample_id, **round_record, "status": "待验收"}
        return self._idem(f"sample_round:{sample_id}:{callback_id or ''}", idem, go)

    def verify_sample(self, sample_id: str, accepted: bool, reviewer: str,
                      reason: str = "", idem: str | None = None) -> dict:
        def go():
            sample = self._require("samples", sample_id)
            if sample["status"] != "待验收":
                raise DomainError(f"样品当前 {sample['status']}，没有待验收的提交")
            current = sample["rounds"][-1]
            if accepted:
                current["verdict"] = "验收通过"
                sample["status"] = "验收通过"
            else:
                current["verdict"] = "已驳回"
                current["reject_reason"] = reason
                sample["status"] = "已驳回"
            self._event("sample_verified", "sample", sample_id,
                        {"round": current["round"], "verdict": current["verdict"],
                         "reason": reason})
            return {"sample_id": sample_id, "status": sample["status"],
                    "round": current["round"], "verdict": current["verdict"]}
        return self._idem(f"sample_verify:{sample_id}", idem, go)

    def seal_sample(self, sample_id: str, idem: str | None = None) -> dict:
        def go():
            sample = self._require("samples", sample_id)
            if sample["status"] != "验收通过":
                raise DomainError("只有验收通过的样品可以封样")
            sample["status"] = "已封样"
            self._event("sample_sealed", "sample", sample_id, {})
            return {"sample_id": sample_id, "status": "已封样"}
        return self._idem("sample_seal", idem, go)

    # ------------------------------------------------------------------ 商品、排产、销售

    def create_product(self, title: str, design_shares: list[dict],
                       idem: str | None = None) -> dict:
        """design_shares: [{"design_id", "share"}]，占比之和为 1。"""
        def go():
            total = Decimal("0")
            for item in design_shares:
                self._require("designs", item["design_id"])
                total += Decimal(str(item["share"]))
            if total != Decimal("1"):
                raise DomainError(f"商品内设计占比之和必须为 1，当前 {total}")
            pid = self._id("prod")
            product = {
                "id": pid, "title": title, "design_shares": design_shares,
                "status": "筹备中", "at": utcnow(),
            }
            self._store["products"][pid] = product
            self._event("product_created", "product", pid, {"title": title})
            return product
        return self._idem("product", idem, go)

    def create_production_batch(self, product_id: str, qty: int,
                                status: str = "未排产", idem: str | None = None) -> dict:
        def go():
            self._require("products", product_id)
            self._vocab("生产批次状态", status)
            if qty <= 0:
                raise DomainError("数量必须为正整数")
            bpid = self._id("pbatch")
            record = {"id": bpid, "product_id": product_id, "qty": qty,
                      "status": status, "at": utcnow()}
            self._store["production_batches"][bpid] = record
            self._event("production_batch_created", "production_batch", bpid,
                        {"product_id": product_id, "qty": qty, "status": status})
            return record
        return self._idem("pbatch", idem, go)

    def update_production_batch(self, batch_id: str, status: str,
                                idem: str | None = None) -> dict:
        def go():
            record = self._require("production_batches", batch_id)
            self._vocab("生产批次状态", status)
            old = record["status"]
            record["status"] = status
            self._event("production_batch_updated", "production_batch", batch_id,
                        {"from": old, "to": status})
            return record
        return self._idem(f"pbatch_upd:{batch_id}:{status}", idem, go)

    def record_sale(self, product_id: str, qty: int, channel: str,
                    order_ref: str = "", idem: str | None = None) -> dict:
        def go():
            product = self._require("products", product_id)
            if product["status"] == "召回冻结":
                raise DomainError("商品处于召回冻结，争议/撤回处置完成前禁止销售")
            sid = self._id("sale")
            record = {"id": sid, "product_id": product_id, "qty": qty,
                      "channel": channel, "order_ref": order_ref, "at": utcnow()}
            self._store["sales"].append(record)
            self._event("sale_recorded", "product", product_id,
                        {"qty": qty, "channel": channel, "order_ref": order_ref})
            return record
        return self._idem(f"sale:{order_ref}" if order_ref else "sale", idem, go)

    def activate_product(self, product_id: str, idem: str | None = None) -> dict:
        def go():
            product = self._require("products", product_id)
            for item in product["design_shares"]:
                design = self._store["designs"][item["design_id"]]
                if design["status"] != "已确认":
                    raise DomainError(f"设计 {design['id']} 状态 {design['status']}，商品不能上架")
            lic = self._store["licenses"].get(product_id)
            if lic is None or lic["status"] != "有效":
                raise DomainError("商品缺少有效授权版本")
            product["status"] = "在售"
            self._event("product_activated", "product", product_id,
                        {"license_version": lic["version"]})
            return product
        return self._idem("product_activate", idem, go)

    def revise_product_design(self, product_id: str, design_shares: list[dict],
                              scopes: list[str], reason: str,
                              idem: str | None = None) -> dict:
        """撤回/争议后的恢复路径：以不含问题素材的新设计版本替换，重新确权并升级授权版本。"""
        def go():
            product = self._require("products", product_id)
            total = Decimal("0")
            for item in design_shares:
                design = self._require("designs", item["design_id"])
                if design["status"] != "已确认":
                    raise DomainError(f"新设计版本 {design['id']} 必须先完成权属确认")
                total += Decimal(str(item["share"]))
            if total != Decimal("1"):
                raise DomainError(f"商品内设计占比之和必须为 1，当前 {total}")
            product["design_shares"] = design_shares
            for scope in scopes:
                self._vocab("用途范围", scope)
            snapshot = self._product_snapshot(product_id)
            snap_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            current = self._store["licenses"].get(product_id)
            new_version = {
                "product_id": product_id,
                "version": (current["version"] + 1) if current else 1,
                "scopes": scopes,
                "channels": current["channels"] if current else [],
                "terms": current["terms"] if current else "",
                "status": "有效", "snapshot": snapshot, "snapshot_hash": snap_hash,
                "supersedes": current["version"] if current else None,
                "change": "版本更新", "revision_reason": reason, "at": utcnow(),
            }
            if current:
                if current["status"] == "有效":
                    current["status"] = "已被新版本替代"
                self._store["license_versions"].setdefault(product_id, []).append(new_version)
            else:
                self._store["license_versions"].setdefault(product_id, []).append(new_version)
            self._store["licenses"][product_id] = new_version
            product["status"] = "筹备中"
            self._event("product_revised", "product", product_id,
                        {"reason": reason, "snapshot_hash": snap_hash,
                         "version": new_version["version"]})
            return new_version
        return self._idem("product_revise", idem, go)

    # ------------------------------------------------------------------ 授权版本

    def _product_snapshot(self, product_id: str) -> list[dict]:
        """按最终确认的设计占比 × 素材占比，生成商品级权属快照。"""
        product = self._require("products", product_id)
        snapshot = []
        for item in product["design_shares"]:
            did, dshare = item["design_id"], Decimal(str(item["share"]))
            prov = self.provenance(did)
            for mat in prov["materials"]:
                snapshot.append({
                    "design_id": did,
                    "material_id": mat["material_id"],
                    "contributor_id": mat["contributor_id"],
                    "weight": float((dshare * Decimal(str(mat["weight"])))
                                    .quantize(Decimal("0.0001"))),
                    "scopes_granted": mat["scopes"],
                })
        return snapshot

    def issue_license(self, product_id: str, scopes: list[str], channels: list[str],
                      terms: str = "", idem: str | None = None) -> dict:
        def go():
            product = self._require("products", product_id)
            for scope in scopes:
                self._vocab("用途范围", scope)
            for item in product["design_shares"]:
                design = self._store["designs"][item["design_id"]]
                if design["status"] != "已确认":
                    raise DomainError(f"设计 {design['id']} 尚未权属确认")
                gaps = self._clearance_gaps(design["id"], scopes)
                if gaps:
                    raise DomainError("授权范围存在权属缺口："
                                      + json.dumps(gaps, ensure_ascii=False),
                                      code="clearance_failed")
            snapshot = self._product_snapshot(product_id)
            snap_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            old = self._store["licenses"].get(product_id)
            license_record = {
                "product_id": product_id, "version": (old["version"] + 1) if old else 1,
                "scopes": scopes, "channels": channels, "terms": terms,
                "status": "有效", "snapshot": snapshot, "snapshot_hash": snap_hash,
                "supersedes": old["version"] if old else None, "at": utcnow(),
            }
            if old:
                old["status"] = "已被新版本替代"
            self._store["licenses"][product_id] = license_record
            self._store["license_versions"].setdefault(product_id, []).append(license_record)
            product["status"] = "在售" if product["status"] == "筹备中" else product["status"]
            self._event("license_issued", "product", product_id,
                        {"version": license_record["version"], "scopes": scopes,
                         "snapshot_hash": snap_hash,
                         "supersedes": license_record["supersedes"]})
            return license_record
        return self._idem("license_issue", idem, go)

    def update_license(self, product_id: str, action: str, scopes: list[str],
                       channels: list[str] | None = None, terms: str = "",
                       idem: str | None = None) -> dict:
        """授权版本更新：范围扩展/缩减/版本更新，旧版留痕，账目按新快照重算。"""
        def go():
            current = self._store["licenses"].get(product_id)
            if current is None or current["status"] != "有效":
                raise DomainError("商品没有有效的授权版本")
            if action not in {"范围扩展", "范围缩减", "版本更新"}:
                raise DomainError("授权更新动作必须是 范围扩展/范围缩减/版本更新")
            for scope in scopes:
                self._vocab("用途范围", scope)
            merged_channels = channels if channels is not None else current["channels"]
            # 缩减时只校验保留范围；扩展时新范围必须全部通过权属确认
            for item in self._require("products", product_id)["design_shares"]:
                gaps = self._clearance_gaps(item["design_id"], scopes)
                if gaps:
                    raise DomainError("新版本存在权属缺口："
                                      + json.dumps(gaps, ensure_ascii=False),
                                      code="clearance_failed")
            snapshot = self._product_snapshot(product_id)
            snap_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            new_version = {
                "product_id": product_id, "version": current["version"] + 1,
                "scopes": scopes, "channels": merged_channels, "terms": terms or current["terms"],
                "status": "有效", "snapshot": snapshot, "snapshot_hash": snap_hash,
                "supersedes": current["version"], "change": action, "at": utcnow(),
            }
            current["status"] = "受限" if action == "范围缩减" else "已被新版本替代"
            self._store["licenses"][product_id] = new_version
            self._store["license_versions"].setdefault(product_id, []).append(new_version)
            self._event("license_updated", "product", product_id,
                        {"action": action, "from_version": current["version"],
                         "to_version": new_version["version"], "scopes": scopes,
                         "old_snapshot_hash": current["snapshot_hash"],
                         "new_snapshot_hash": snap_hash})
            return new_version
        return self._idem(f"license_upd:{product_id}", idem, go)

    # ------------------------------------------------------------------ 供应商订单与受控下载

    def create_order(self, product_id: str, supplier_id: str, qty: int,
                     idem: str | None = None) -> dict:
        def go():
            product = self._require("products", product_id)
            if product["status"] == "召回冻结":
                raise DomainError("商品处于召回冻结，不能向供应商下达新订单")
            oid = self._id("order")
            order = {"id": oid, "product_id": product_id, "supplier_id": supplier_id,
                     "qty": qty, "status": "已下达", "at": utcnow()}
            self._store["orders"][oid] = order
            self._event("order_placed", "order", oid,
                        {"product_id": product_id, "supplier_id": supplier_id, "qty": qty})
            return order
        return self._idem("order", idem, go)

    def issue_download_grant(self, order_id: str, file_variant: str,
                             max_downloads: int, expires_at: str,
                             idem: str | None = None) -> dict:
        """供应商只能取得其订单所需的可水印文件，受次数与期限限制。"""
        def go():
            order = self._require("orders", order_id)
            product = self._require("products", order["product_id"])
            if product["status"] == "召回冻结":
                raise DomainError("商品处于召回冻结，不能放行生产文件")
            license_record = self._store["licenses"].get(product["id"])
            if license_record is None or license_record["status"] != "有效":
                raise DomainError("商品授权无效，不能放行生产文件")
            if max_downloads <= 0:
                raise DomainError("下载次数上限必须为正整数")
            normalized_expiry = _norm_dt(expires_at)
            if normalized_expiry <= utcnow():
                raise DomainError("到期时间必须晚于当前时间")
            gid = self._id("grant")
            watermark = {
                "order_id": order_id,
                "supplier_id": order["supplier_id"],
                "product_id": product["id"],
                "license_version": license_record["version"],
                "issued_seq": self.seq + 1,
            }
            grant = {
                "id": gid, "order_id": order_id, "file_variant": file_variant,
                "watermarked": True, "watermark": watermark,
                "max_downloads": max_downloads, "downloads": 0,
                "expires_at": normalized_expiry, "status": "有效", "at": utcnow(),
            }
            self._store["download_grants"][gid] = grant
            self._event("download_granted", "download_grant", gid,
                        {"order_id": order_id, "variant": file_variant,
                         "max_downloads": max_downloads, "expires_at": expires_at})
            return grant
        return self._idem("grant", idem, go)

    def fetch_file(self, grant_id: str, idem: str | None = None) -> dict:
        def go():
            grant = self._require("download_grants", grant_id)
            if grant["status"] != "有效":
                raise DomainError(f"下载授权状态 {grant['status']}")
            if utcnow() > grant["expires_at"]:
                grant["status"] = "已过期"
                self._event("download_expired", "download_grant", grant_id, {})
                raise DomainError("下载授权已过期", code="grant_expired")
            if grant["downloads"] >= grant["max_downloads"]:
                raise DomainError("下载次数已用尽", code="grant_exhausted")
            grant["downloads"] += 1
            token = hashlib.sha256(
                f"{grant_id}:{grant['downloads']}:{json.dumps(grant['watermark'], sort_keys=True)}"
                .encode()).hexdigest()[:16]
            self._event("file_fetched", "download_grant", grant_id,
                        {"n": grant["downloads"], "token": token})
            return {
                "grant_id": grant_id, "file_variant": grant["file_variant"],
                "watermarked": True, "watermark": grant["watermark"],
                "download": grant["downloads"], "max_downloads": grant["max_downloads"],
                "expires_at": grant["expires_at"], "token": token,
            }
        return self._idem(f"fetch:{grant_id}", idem, go)

    # ------------------------------------------------------------------ 撤回 / 争议与影响分析

    def _downstream(self, material_id: str) -> dict:
        """沿来源图找出受影响的设计、商品、排产批次、订单与已售数量。"""
        affected_designs = []
        for did in self._store["designs"]:
            prov = self.provenance(did)
            if any(m["material_id"] == material_id for m in prov["materials"]):
                affected_designs.append(did)
        products, pb_unmade, pb_inprogress, sold_qty, open_orders = [], [], [], 0, []
        for pid, product in self._store["products"].items():
            linked = [d for d in affected_designs
                      if any(x["design_id"] == d for x in product["design_shares"])]
            if not linked:
                continue
            products.append(pid)
            for pb in self._store["production_batches"].values():
                if pb["product_id"] != pid:
                    continue
                if pb["status"] == "未排产":
                    pb_unmade.append(pb["id"])
                elif pb["status"] in {"生产中", "已入库"}:
                    pb_inprogress.append(pb["id"])
            sold_qty += sum(s["qty"] for s in self._store["sales"]
                            if s["product_id"] == pid)
            for order in self._store["orders"].values():
                if order["product_id"] == pid and order["status"] == "已下达":
                    open_orders.append(order["id"])
        return {
            "designs": affected_designs, "products": products,
            "unmade_batches": pb_unmade, "inprogress_batches": pb_inprogress,
            "sold_qty": sold_qty, "open_orders": open_orders,
        }

    def impact_analysis(self, material_id: str) -> dict:
        self._require("materials", material_id)
        with self.lock:
            impact = self._downstream(material_id)
            return {"material_id": material_id,
                    "material_status": self._store["materials"][material_id]["status"],
                    **impact,
                    "policy": {
                        "尚未生产": "可直接撤单/改版，未投产批次停止排产",
                        "在制": "冻结在制批次与未结订单，等待替换版本或授权恢复",
                        "已售出": "不删除销售记录，按召回/补偿流程处置并追溯分配",
                    }}

    def _freeze_downstream(self, material_id: str, material_status: str,
                           design_status: str, product_status: str, etype: str, reason: str,
                           scopes: set[str] | None = None):
        """scopes=None 表示全范围撤回；否则只冻结确权范围与之相交的设计。"""
        material = self._store["materials"][material_id]
        material["status"] = material_status
        impact = self._downstream(material_id)
        frozen_designs = []
        for did in impact["designs"]:
            design = self._store["designs"][did]
            review_scopes = set((design.get("review") or {}).get("scopes", []))
            hit = scopes is None or (review_scopes & scopes) or not review_scopes
            if hit and design["status"] != "已撤回":
                design["status"] = design_status
                frozen_designs.append(did)
        frozen_products = []
        for pid in impact["products"]:
            product = self._store["products"][pid]
            linked = [d for d in frozen_designs
                      if any(x["design_id"] == d for x in product["design_shares"])]
            if not linked:
                continue
            frozen_products.append(pid)
            product["status"] = product_status
            lic = self._store["licenses"].get(pid)
            if lic and lic["status"] == "有效":
                lic["status"] = "受限"
            for grant in self._store["download_grants"].values():
                order = self._store["orders"][grant["order_id"]]
                if order["product_id"] == pid and grant["status"] == "有效":
                    grant["status"] = "已废止"
        self._event(etype, "material", material_id,
                    {"reason": reason, "scopes": sorted(scopes) if scopes else "ALL",
                     "impact": {**impact, "frozen_designs": frozen_designs,
                                "frozen_products": frozen_products}})
        return impact

    def withdraw_material(self, material_id: str, reason: str, scope: str | None = None,
                          idem: str | None = None) -> dict:
        def go():
            material = self._require("materials", material_id)
            scopes = [scope] if scope else [g["scope"] for g in material["grants"]
                                            if g["status"] == "有效"]
            for sc in scopes:
                self._vocab("用途范围", sc)
                for grant in material["grants"]:
                    if grant["scope"] == sc and grant["status"] == "有效":
                        grant["status"] = "已撤回"
                        self._event("scope_withdrawn", "material", material_id,
                                    {"scope": sc, "reason": reason})
            if not any(g["status"] == "有效" for g in material["grants"]):
                impact = self._freeze_downstream(
                    material_id, "已撤回", "已撤回", "召回冻结",
                    "material_withdrawn", reason)
            else:
                # 部分范围撤回：只冻结依赖被撤范围（如商业许可）的设计
                impact = self._freeze_downstream(
                    material_id, "受限", "受限冻结", "召回冻结",
                    "material_scope_withdrawn", reason, scopes=set(scopes))
            return {"material_id": material_id, "withdrawn_scopes": scopes,
                    "status": material["status"], "impact": impact}
        return self._idem(f"withdraw:{material_id}:{scope or 'ALL'}", idem, go)

    def open_dispute(self, material_id: str, reason: str, idem: str | None = None) -> dict:
        def go():
            self._require("materials", material_id)
            impact = self._freeze_downstream(
                material_id, "争议中", "争议冻结", "召回冻结",
                "dispute_opened", reason)
            return {"material_id": material_id, "status": "争议中", "impact": impact}
        return self._idem(f"dispute_open:{material_id}", idem, go)

    def resolve_dispute(self, material_id: str, resolution: str,
                        idem: str | None = None) -> dict:
        def go():
            material = self._require("materials", material_id)
            if material["status"] != "争议中":
                raise DomainError("该素材不在争议中")
            material["status"] = "有效" if any(
                g["status"] == "有效" for g in material["grants"]) else "待确认"
            for did, design in self._store["designs"].items():
                if design["status"] == "争议冻结":
                    design["status"] = "待权属确认"  # 争议解除后须重新确权
            self._event("dispute_resolved", "material", material_id,
                        {"resolution": resolution})
            return {"material_id": material_id, "status": material["status"],
                    "note": "受影响设计需重新权属确认后商品方可重新上架"}
        return self._idem(f"dispute_resolve:{material_id}", idem, go)

    # ------------------------------------------------------------------ 结算分配

    def create_settlement_run(self, product_id: str, period: str, amount,
                              idem: str | None = None) -> dict:
        """按最终确认的贡献比例生成可追溯分配（锁定授权版本与权属快照）。"""
        def go():
            product = self._require("products", product_id)
            lic = self._store["licenses"].get(product_id)
            if lic is None or lic["status"] != "有效":
                raise DomainError("商品无有效授权，不能结算")
            total_cents = _cents(amount)
            if total_cents <= 0:
                raise DomainError("结算金额必须为正数")
            snapshot = self._product_snapshot(product_id)
            live_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            # 来源图与授权快照不一致（例如授权版本更新后比例变化）必须先核对
            if live_hash != lic["snapshot_hash"]:
                raise DomainError(
                    f"来源图已变化（live={live_hash}，license v{lic['version']}="
                    f"{lic['snapshot_hash']}），请基于最新授权版本重开结算批次",
                    code="provenance_drift")
            aggregated: dict[str, dict] = {}
            for row in snapshot:
                bucket = aggregated.setdefault(row["contributor_id"], {
                    "contributor_id": row["contributor_id"],
                    "weight": Decimal("0"), "materials": set()})
                bucket["weight"] += Decimal(str(row["weight"]))
                bucket["materials"].add(row["material_id"])
            if not aggregated:
                raise DomainError("快照中没有可分配的贡献者")

            runs = self._store["royalty_runs"]
            rid = self._id("run")
            entries, allocated = [], 0
            rows = sorted(aggregated.values(), key=lambda r: r["contributor_id"])
            # 先按比例分，尾差给占比最大的贡献者，保证账目分毫不差
            shares = [(r, total_cents * r["weight"]) for r in rows]
            shares.sort(key=lambda x: x[1], reverse=True)
            raw = [(r, int(s.to_integral_value(rounding=ROUND_HALF_UP))) for r, s in shares]
            remainder = total_cents - sum(c for _, c in raw)
            raw[0] = (raw[0][0], raw[0][1] + remainder)
            for row, cents in raw:
                allocated += cents
                entries.append({
                    "contributor_id": row["contributor_id"],
                    "amount": _money(cents),
                    "weight": float(row["weight"].quantize(Decimal("0.0001"))),
                    "materials": sorted(row["materials"]),
                    "license_version": lic["version"],
                    "snapshot_hash": lic["snapshot_hash"],
                    "status": "待结算",
                })
            run = {
                "id": rid, "product_id": product_id, "period": period,
                "amount": _money(total_cents), "currency": "CNY",
                "license_version": lic["version"], "snapshot_hash": lic["snapshot_hash"],
                "status": "待结算", "entries": entries, "at": utcnow(),
            }
            assert allocated == total_cents
            runs[rid] = run
            self._event("settlement_created", "royalty_run", rid,
                        {"product_id": product_id, "amount": run["amount"],
                         "license_version": lic["version"],
                         "snapshot_hash": lic["snapshot_hash"]})
            return run
        return self._idem("run", idem, go)

    def lock_settlement(self, run_id: str, idem: str | None = None) -> dict:
        def go():
            run = self._require("royalty_runs", run_id)
            if run["status"] != "待结算":
                return {"run_id": run_id, "status": run["status"], "unchanged": True}
            run["status"] = "已锁定"
            for entry in run["entries"]:
                entry["status"] = "已锁定"
            self._event("settlement_locked", "royalty_run", run_id, {})
            return {"run_id": run_id, "status": "已锁定"}
        return self._idem("run_lock", idem, go)

    def payment_callback(self, run_id: str, callback_id: str,
                         idem: str | None = None) -> dict:
        """支付/结算回调。同一 callback_id 重复投递只分配一次，账目保持不变。"""
        def go():
            run = self._require("royalty_runs", run_id)
            if run["status"] == "已分配":
                return {"run_id": run_id, "status": "已分配", "amount": run["amount"],
                        "unchanged": True}
            if run["status"] != "已锁定":
                raise DomainError("结算批次未锁定，拒绝支付回调")
            run["status"] = "已分配"
            run["paid_callback_id"] = callback_id
            for entry in run["entries"]:
                entry["status"] = "已分配"
            self._event("settlement_distributed", "royalty_run", run_id,
                        {"callback_id": callback_id, "amount": run["amount"]})
            return {"run_id": run_id, "status": "已分配", "amount": run["amount"],
                    "entries": len(run["entries"])}
        return self._idem(f"pay_callback:{run_id}:{callback_id}", idem, go)

    # ------------------------------------------------------------------ 查询与自检

    def consistency_report(self) -> dict:
        """核对账目与来源图/授权状态的一致性。"""
        problems = []
        for gid, grant in self._store["download_grants"].items():
            if grant["status"] == "有效" and grant["downloads"] > grant["max_downloads"]:
                problems.append({"type": "下载次数超限", "grant_id": gid})
            if grant["status"] == "有效" and utcnow() > grant["expires_at"]:
                problems.append({"type": "有效授权已过期未处理", "grant_id": gid})
        for pid, lic in self._store["licenses"].items():
            if lic["status"] != "有效":
                continue
            live = self._product_snapshot(pid)
            live_hash = hashlib.sha256(
                json.dumps(live, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            if live_hash != lic["snapshot_hash"]:
                problems.append({"type": "授权快照与来源图漂移", "product_id": pid,
                                 "license_version": lic["version"],
                                 "live_hash": live_hash, "license_hash": lic["snapshot_hash"]})
        for rid, run in self._store["royalty_runs"].items():
            total = _cents(run["amount"])
            entries_total = sum(_cents(e["amount"]) for e in run["entries"])
            if total != entries_total:
                problems.append({"type": "分配金额不平", "run_id": rid,
                                 "amount": total, "entries": entries_total})
            current_lic = self._store["licenses"].get(run["product_id"])
            if (run["status"] == "待结算" and current_lic
                    and current_lic["status"] == "有效"
                    and run["snapshot_hash"] != current_lic["snapshot_hash"]):
                problems.append({"type": "待结算批次与现行授权版本不一致", "run_id": rid,
                                 "run_hash": run["snapshot_hash"],
                                 "license_hash": current_lic["snapshot_hash"]})
        return {"ok": not problems, "problems": problems,
                "counts": {k: (len(v) if isinstance(v, dict) else len(v))
                           for k, v in self._store.items() if k not in {"events", "idempotency"}}}

    def events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[dict]:
        result = self._store["events"]
        if entity_type:
            result = [e for e in result if e["entity_type"] == entity_type]
        if entity_id:
            result = [e for e in result if e["entity_id"] == entity_id]
        return result


class _ClearanceFailure(Exception):
    def __init__(self, gaps):
        self.gaps = gaps
