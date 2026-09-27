"""文创设计权利转换服务。

承接创意简报、素材贡献、生成批次、人工修改、权属确认、样品验收与商品授权，
核心是两张互相对账的图：

* 来源图（provenance）：每个设计版本记录父版本与所用素材；
* 账目（ledger）：生成回调、打样结论、授权更新、结算等所有状态变化都是
  幂等追加事件。重复回调、打样驳回重提、授权版本更新都不会让两者漂移。

关键规则：

* 未成年人作品必须经监护确认；
* 校园展示许可不自动扩展为商业许可，商品生产授权逐项授予；
* 撤回/争议不删除记录，而是按 尚未生产 / 在制 / 已售出 给出差异化影响。
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SERVICE_ID = "creative-rights-conversion"
DOMAIN_PATH = Path(__file__).with_name("domain.json")

展示类用途 = frozenset({"校内展示", "公益展览"})


class ServiceError(Exception):
    """违反领域规则时抛出，HTTP 层映射为 4xx。"""

    def __init__(self, message: str, code: str = "规则冲突", status: int = 409):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def today() -> str:
    return date.today().isoformat()


def health() -> dict:
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


# --------------------------------------------------------------------------- #
# 领域配置
# --------------------------------------------------------------------------- #
def load_domain(path: Path = DOMAIN_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["贡献类型", "用途范围", "授权状态", "版本类型", "样品状态", "商品生产状态"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"domain.json 缺少枚举：{missing}")
    return data


# --------------------------------------------------------------------------- #
# 核心服务
# --------------------------------------------------------------------------- #
class CreativeRightsService:
    """内存实现；所有写方法对同一把锁串行化，保证账目与来源图原子一致。"""

    def __init__(self, domain: dict | None = None):
        self.domain = domain or load_domain()
        self._lock = threading.RLock()
        self.briefs: dict[str, dict] = {}
        self.contributors: dict[str, dict] = {}
        self.contributions: dict[str, dict] = {}
        self.generation_batches: dict[str, dict] = {}
        self.versions: dict[str, dict] = {}
        self.ownership_claims: dict[str, dict] = {}
        self.samples: dict[str, dict] = {}
        self.license_versions: dict[str, list[dict]] = {}
        self.products: dict[str, dict] = {}
        self.suppliers: dict[str, dict] = {}
        self.file_grants: dict[str, dict] = {}
        self.file_downloads: list[dict] = []
        self.settlements: dict[str, dict] = {}
        self.events: list[dict] = []
        # 幂等键 -> 事件 ID，重复投递直接回放首次结果
        self.idempotency: dict[str, str] = {}
        # 事件类型 -> 处理函数（在锁内执行）
        self._reducers = {
            "贡献登记": self._apply_contribution,
            "监护确认": self._apply_guardian,
            "用途授权更新": self._apply_purpose_grant,
            "生成回调": self._apply_generation,
            "版本合并": self._apply_version_merge,
            "权属确认": self._apply_ownership,
            "打样提交": self._apply_sample_submit,
            "样品验收": self._apply_sample_review,
            "授权版本更新": self._apply_license_update,
            "商品登记": self._apply_product,
            "生产推进": self._apply_production,
            "文件发放": self._apply_file_grant,
            "文件下载": self._apply_file_download,
            "结算生成": self._apply_settlement,
            "撤回": self._apply_withdraw,
            "争议": self._apply_dispute,
        }

    # ---------------- 基础工具 ---------------- #
    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def _record_event(self, etype: str, payload: dict, idem_key: str | None) -> dict:
        """追加账目事件并调用对应 reducer。同一 idem_key 只生效一次。

        返回 reducer 结果并附“重复”标志，供调用方判断是否为重放。
        """
        with self._lock:
            if idem_key is not None and idem_key in self.idempotency:
                first = self.events_by_id[self.idempotency[idem_key]]
                return {**first["结果"], "重复": True}
            event = {
                "id": self._new_id("evt"),
                "时间": datetime.now().isoformat(timespec="seconds"),
                "类型": etype,
                "幂等键": idem_key,
                "载荷": payload,
            }
            result = self._reducers[etype](payload)
            event["结果"] = result
            self.events.append(event)
            if idem_key is not None:
                self.idempotency[idem_key] = event["id"]
            return {**result, "重复": False}

    @property
    def events_by_id(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.events}

    def _get(self, store: dict, key: str, label: str) -> dict:
        if key not in store:
            raise ServiceError(f"未知{label}：{key}", "未找到", 404)
        return store[key]

    def _check_enum(self, field: str, value: str, enum_key: str):
        if value not in self.domain[enum_key]:
            raise ServiceError(
                f"{field}={value!r} 不在 {enum_key} {self.domain[enum_key]} 中",
                "非法枚举", 400,
            )

    # ---------------- 1. 创意简报 ---------------- #
    def create_brief(self, title: str, summary: str, idem_key: str | None = None) -> dict:
        with self._lock:
            if idem_key is not None and idem_key in self.idempotency:
                event = self.events_by_id[self.idempotency[idem_key]]
                return self.briefs[event["载荷"]["简报"]]
            brief_id = self._new_id("brief")
            self.briefs[brief_id] = {
                "id": brief_id, "标题": title, "摘要": summary,
                "创建于": today(), "状态": "征集中",
            }
            self._append_raw_event(
                "简报创建", {"简报": brief_id, "标题": title, "摘要": summary}, idem_key)
        return self.briefs[brief_id]

    def _append_raw_event(self, etype: str, payload: dict, idem_key: str | None):
        if idem_key is not None and idem_key in self.idempotency:
            return
        event = {
            "id": self._new_id("evt"),
            "时间": datetime.now().isoformat(timespec="seconds"),
            "类型": etype, "幂等键": idem_key, "载荷": payload,
        }
        self.events.append(event)
        if idem_key is not None:
            self.idempotency[idem_key] = event["id"]

    # ---------------- 2. 贡献者与素材贡献 ---------------- #
    def register_contributor(self, name: str, minor: bool = False) -> dict:
        cid = self._new_id("person")
        with self._lock:
            self.contributors[cid] = {
                "id": cid, "姓名": name, "是否未成年": minor,
                "监护确认": None, "学校": None,
            }
            self._append_raw_event("贡献者登记",
                                   {"贡献者": cid, "姓名": name, "未成年": minor}, None)
        return self.contributors[cid]

    def guardian_confirm(self, contributor_id: str, guardian: str,
                         idem_key: str | None = None) -> dict:
        return self._record_event("监护确认", {
            "贡献者": contributor_id, "监护人": guardian,
        }, idem_key)

    def _apply_guardian(self, p: dict) -> dict:
        person = self._get(self.contributors, p["贡献者"], "贡献者")
        if not person["是否未成年"]:
            raise ServiceError("贡献者不是未成年人，无需监护确认", "规则冲突")
        person["监护确认"] = {"监护人": p["监护人"], "确认于": today()}
        # 监护确认可能解锁此前登记的素材，统一重算
        for c in self.contributions.values():
            if c["贡献者"] == person["id"]:
                self._refresh_contribution_state(c)
        return {"贡献者": person["id"], "监护确认": person["监护确认"]}

    def submit_contribution(self, brief_id: str, contributor_id: str, ctype: str,
                            title: str, declared_sources: list[str] | None = None,
                            idem_key: str | None = None) -> dict:
        """登记一份素材贡献。授权状态一律从“待确认”起步。"""
        self._get(self.briefs, brief_id, "简报")
        self._get(self.contributors, contributor_id, "贡献者")
        self._check_enum("贡献类型", ctype, "贡献类型")
        return self._record_event("贡献登记", {
            "简报": brief_id, "贡献者": contributor_id, "类型": ctype,
            "标题": title, "申报来源": declared_sources or [],
        }, idem_key)

    def _apply_contribution(self, p: dict) -> dict:
        cid = self._new_id("contrib")
        person = self.contributors[p["贡献者"]]
        ready = (not person["是否未成年"]) or bool(person["监护确认"])
        # 用途逐项授权：缺省全部“待确认”；展示类也必须显式授予，不做任何推断。
        purposes = {u: "待确认" for u in self.domain["用途范围"]}
        self.contributions[cid] = {
            "id": cid, "简报": p["简报"], "贡献者": p["贡献者"],
            "类型": p["类型"], "标题": p["标题"],
            "申报来源": p["申报来源"],
            "授权状态": "待确认",
            "用途授权": purposes,
            "可商用": False,
            "可用于设计": False,
            "监护就绪": ready,
        }
        return {"贡献": cid, "授权状态": "待确认", "监护就绪": ready}

    def grant_purpose(self, contribution_id: str, purpose: str, state: str,
                      idem_key: str | None = None) -> dict:
        """逐项授予/收窄某一用途。展示许可与商用许可彼此独立。"""
        self._check_enum("用途范围", purpose, "用途范围")
        self._check_enum("用途授权状态", state, "用途授权状态")
        return self._record_event("用途授权更新", {
            "贡献": contribution_id, "用途": purpose, "状态": state,
        }, idem_key)

    def _apply_purpose_grant(self, p: dict) -> dict:
        c = self._get(self.contributions, p["贡献"], "贡献")
        c["用途授权"][p["用途"]] = p["状态"]
        self._refresh_contribution_state(c)
        return {"贡献": c["id"], "用途": p["用途"], "状态": p["状态"],
                "可商用": c["可商用"], "授权状态": c["授权状态"]}

    def _refresh_contribution_state(self, c: dict) -> None:
        """依据用途授权与监护状态重算汇总标志（展示与商用彼此独立）。"""
        grants = c["用途授权"]
        person = self.contributors[c["贡献者"]]
        guardian_ready = (not person["是否未成年"]) or bool(person["监护确认"])
        c["监护就绪"] = guardian_ready
        c["可商用"] = grants["商品生产"] == "有效"
        blocked = any(v in ("已撤回", "争议中") for v in grants.values())
        c["可用于设计"] = (guardian_ready and not blocked
                          and any(v == "有效" for v in grants.values()))
        states = list(grants.values())
        if any(v == "争议中" for v in states):
            summary = "争议中"
        elif all(v == "有效" for v in states):
            summary = "有效"
        elif any(v == "有效" for v in states):
            summary = "受限"
        elif any(v == "已撤回" for v in states):
            summary = "已撤回"
        else:
            summary = "待确认"
        c["授权状态"] = summary

    # ---------------- 3. 生成批次（机器辅助设计） ---------------- #
    def register_generation_batch(self, brief_id: str, prompt: str,
                                  model_profile: str,
                                  input_contributions: list[str] | None = None) -> dict:
        """创建批次（待回调）。同一回调幂等键重复投递只产生一个产出版本。"""
        self._get(self.briefs, brief_id, "简报")
        for cid in input_contributions or []:
            c = self._get(self.contributions, cid, "贡献")
            if not c["可用于设计"]:
                raise ServiceError(
                    f"素材 {c['标题']} 当前不可用于设计（授权 {c['授权状态']}）",
                    "素材不可用")
        bid = self._new_id("batch")
        with self._lock:
            self.generation_batches[bid] = {
                "id": bid, "简报": brief_id, "提示词": prompt,
                "模型": model_profile,
                "输入贡献": list(input_contributions or []),
                "回调状态": "待回调", "产出版本": None,
                "回调次数": 0,
            }
            self._append_raw_event("生成批次登记", {"批次": bid, "简报": brief_id}, None)
        return self.generation_batches[bid]

    def generation_callback(self, batch_id: str, ok: bool, output_ref: str | None,
                            idem_key: str, detail: str = "") -> dict:
        """机器生成平台的（可能重复的）完成回调。"""
        self._get(self.generation_batches, batch_id, "生成批次")
        return self._record_event("生成回调", {
            "批次": batch_id, "成功": ok, "产出": output_ref, "说明": detail,
        }, idem_key)

    def _apply_generation(self, p: dict) -> dict:
        batch = self.generation_batches[p["批次"]]
        batch["回调次数"] += 1
        if p["成功"]:
            batch["回调状态"] = "成功"
            if batch["产出版本"] is None:
                vid = self._new_id("ver")
                self.versions[vid] = {
                    "id": vid, "简报": batch["简报"], "类型": "生成批次产出",
                    "父版本": [], "所用素材": list(batch["输入贡献"]),
                    "批次": batch["id"], "产物引用": p["产出"],
                    "创建于": today(), "可用": True,
                }
                batch["产出版本"] = vid
        else:
            batch["回调状态"] = "失败"
        return {"批次": batch["id"], "回调状态": batch["回调状态"],
                "产出版本": batch["产出版本"]}

    # ---------------- 4. 版本合并 / 人工修改（来源图） ---------------- #
    def merge_version(self, brief_id: str, vtype: str, parent_versions: list[str],
                      used_contributions: list[str], note: str = "",
                      idem_key: str | None = None) -> dict:
        """人工修改或合并：必须记录全部父版本与所用素材，来源图闭包可追。"""
        self._get(self.briefs, brief_id, "简报")
        self._check_enum("版本类型", vtype, "版本类型")
        if not parent_versions:
            raise ServiceError("合并/修改必须至少指定一个父版本", "来源缺失", 400)
        for vid in parent_versions:
            self._get(self.versions, vid, "版本")
        contribs = []
        for cid in used_contributions:
            c = self._get(self.contributions, cid, "贡献")
            if not c["可用于设计"]:
                raise ServiceError(
                    f"素材 {c['标题']} 当前不可用于设计（授权 {c['授权状态']}）",
                    "素材不可用")
            contribs.append(cid)
        return self._record_event("版本合并", {
            "简报": brief_id, "类型": vtype, "父版本": parent_versions,
            "所用素材": contribs, "说明": note,
        }, idem_key)

    def _apply_version_merge(self, p: dict) -> dict:
        vid = self._new_id("ver")
        self.versions[vid] = {
            "id": vid, "简报": p["简报"], "类型": p["类型"],
            "父版本": list(p["父版本"]), "所用素材": list(p["所用素材"]),
            "说明": p.get("说明", ""), "创建于": today(), "可用": True,
        }
        return {"版本": vid, "父版本": p["父版本"], "所用素材": p["所用素材"]}

    def provenance(self, version_id: str) -> dict:
        """返回来源闭包：全部祖先版本与传递依赖的素材（去重保序）。"""
        root = self._get(self.versions, version_id, "版本")
        versions, materials = [], []

        def walk(vid: str, seen: set[str]):
            if vid in seen:
                return
            seen.add(vid)
            v = self.versions[vid]
            versions.append(vid)
            for cid in v["所用素材"]:
                if cid not in materials:
                    materials.append(cid)
            for parent in v["父版本"]:
                walk(parent, seen)

        walk(version_id, set())
        return {"版本": root["id"], "祖先版本": versions[1:],
                "含自身": versions, "素材闭包": materials}

    # ---------------- 5. 权属确认 ---------------- #
    def confirm_ownership(self, version_id: str, contributor_shares: dict[str, float],
                          idem_key: str | None = None) -> dict:
        """按最终确认的贡献比例锁定权属；比例必须覆盖来源闭包中的全部素材。"""
        self._get(self.versions, version_id, "版本")
        total = round(sum(contributor_shares.values()), 6)
        if abs(total - 1.0) > 1e-6:
            raise ServiceError(f"贡献比例之和必须为 1，当前 {total}", "比例错误", 400)
        prov = self.provenance(version_id)
        closure_contributors = {self.contributions[c]["贡献者"]
                                for c in prov["素材闭包"]}
        unknown = set(contributor_shares) - set(self.contributors)
        if unknown:
            raise ServiceError(f"未知贡献者：{sorted(unknown)}", "比例错误", 400)
        missing = closure_contributors - set(contributor_shares)
        if missing:
            names = [self.contributors[m]["姓名"] for m in missing]
            raise ServiceError(f"来源闭包中的贡献者未分配比例：{names}", "比例错误", 400)
        extra = set(contributor_shares) - closure_contributors
        if extra:
            names = [self.contributors[m]["姓名"] for m in extra]
            raise ServiceError(f"向无来源贡献的人分配了比例：{names}", "比例错误", 400)
        # 未成年人作品须监护确认；撤回/争议中的素材不能锁定权属。
        # 注意：此处不要求商品生产授权——展示类合作同样可以确认权属；
        # 商用授权在签发授权版本时按 scope 逐项校验。
        for cid in prov["素材闭包"]:
            c = self.contributions[cid]
            person = self.contributors[c["贡献者"]]
            if person["是否未成年"] and not person["监护确认"]:
                raise ServiceError(
                    f"素材 {c['标题']} 来自未成年人且缺少监护确认", "监护缺失")
            if c["授权状态"] in ("已撤回", "争议中"):
                raise ServiceError(
                    f"素材 {c['标题']} 处于{c['授权状态']}，不能确认权属", "权属不清")
        return self._record_event("权属确认", {
            "版本": version_id, "比例": contributor_shares,
        }, idem_key)

    def _apply_ownership(self, p: dict) -> dict:
        claim_id = self._new_id("own")
        self.ownership_claims[p["版本"]] = {
            "id": claim_id, "版本": p["版本"], "比例": dict(p["比例"]),
            "确认于": today(),
        }
        v = self.versions[p["版本"]]
        v["类型"] = "最终确认版"
        return {"权属确认": claim_id, "版本": p["版本"], "比例": p["比例"]}

    def get_ownership(self, version_id: str) -> dict:
        return self._get(self.ownership_claims, version_id, "版本的权属确认")

    # ---------------- 6. 样品验收（驳回可重提） ---------------- #
    def submit_sample(self, version_id: str, supplier_id: str, sample_ref: str,
                      idem_key: str | None = None) -> dict:
        self._get(self.versions, version_id, "版本")
        self._get(self.suppliers, supplier_id, "供应商")
        return self._record_event("打样提交", {
            "版本": version_id, "供应商": supplier_id, "样品": sample_ref,
        }, idem_key)

    def _apply_sample_submit(self, p: dict) -> dict:
        # 同一版本同一供应商的新一轮打样；历史轮次全部保留
        rounds = [s for s in self.samples.values()
                  if s["版本"] == p["版本"] and s["供应商"] == p["供应商"]]
        seq = len(rounds) + 1
        sid = self._new_id("sample")
        self.samples[sid] = {
            "id": sid, "版本": p["版本"], "供应商": p["供应商"],
            "样品引用": p["样品"], "轮次": seq,
            "状态": "待打样", "驳回原因": None, "结论于": None,
        }
        return {"样品": sid, "轮次": seq, "状态": "待打样"}

    def review_sample(self, sample_id: str, approved: bool, reason: str = "",
                      idem_key: str | None = None) -> dict:
        self._get(self.samples, sample_id, "样品")
        return self._record_event("样品验收", {
            "样品": sample_id, "通过": approved, "原因": reason,
        }, idem_key)

    def _apply_sample_review(self, p: dict) -> dict:
        s = self.samples[p["样品"]]
        if s["状态"] in ("已通过", "已驳回"):
            raise ServiceError(f"样品第 {s['轮次']} 轮已结，不能重复验收", "状态冲突")
        s["状态"] = "已通过" if p["通过"] else "已驳回"
        s["驳回原因"] = None if p["通过"] else p["原因"]
        s["结论于"] = today()
        return {"样品": s["id"], "轮次": s["轮次"], "状态": s["状态"],
                "驳回原因": s["驳回原因"]}

    def latest_sample(self, version_id: str, supplier_id: str) -> dict | None:
        rounds = [s for s in self.samples.values()
                  if s["版本"] == version_id and s["供应商"] == supplier_id]
        return max(rounds, key=lambda s: s["轮次"], default=None)

    # ---------------- 7. 授权版本 ---------------- #
    def issue_license(self, version_id: str, scope: list[str],
                      idem_key: str | None = None) -> dict:
        """基于已通过权属确认的版本签发/更新授权版本。

        scope 只能包含该版本素材已获有效授权的用途；授权更新不删除旧版本，
        已发放给商品的旧授权仍可追溯。
        """
        self._get(self.versions, version_id, "版本")
        for u in scope:
            self._check_enum("用途范围", u, "用途范围")
        self.get_ownership(version_id)
        prov = self.provenance(version_id)
        if "商品生产" in scope:
            # 商用授权必须先完成样品验收
            passed = any(s["版本"] == version_id and s["状态"] == "已通过"
                         for s in self.samples.values())
            if not passed:
                raise ServiceError("商品生产授权需以样品验收通过为前提", "样品未通过")
        for u in scope:
            for cid in prov["素材闭包"]:
                if self.contributions[cid]["用途授权"][u] != "有效":
                    raise ServiceError(
                        f"用途 {u} 未获得素材 {self.contributions[cid]['标题']} 的有效授权"
                        "（展示许可不自动扩展商用）",
                        "授权范围越界")
        return self._record_event("授权版本更新", {
            "版本": version_id, "范围": scope,
        }, idem_key)

    def _apply_license_update(self, p: dict) -> dict:
        v = self.versions[p["版本"]]
        revs = self.license_versions.setdefault(p["版本"], [])
        if revs:
            revs[-1]["状态"] = "被替代"
            lid, prev = revs[-1]["id"], revs[-1]["版次"]
            seq = prev + 1
        else:
            lid, prev, seq = self._new_id("lic"), None, 1
        record = {
            "id": lid, "版本": p["版本"], "版次": seq,
            "范围": list(p["范围"]), "签发于": today(), "状态": "有效",
            "上一版": prev,
        }
        revs.append(record)
        v["类型"] = "授权版本"
        return {"授权": lid, "版次": seq, "范围": record["范围"],
                "上一版": prev}

    def get_license(self, version_id: str) -> dict:
        revs = self.license_versions.get(version_id)
        if not revs:
            raise ServiceError(f"版本 {version_id} 尚无授权", "未找到", 404)
        return revs[-1]

    def license_revision(self, license_id: str, revision: int | None = None) -> dict:
        """按授权系列 ID（与版次）取回授权记录；不给版次取最新版。

        旧版次永久保留可追溯：已登记商品钉住各自授权版次，新登记取最新版。
        """
        found = None
        for revs in self.license_versions.values():
            for r in revs:
                if r["id"] == license_id:
                    if revision is None:
                        found = r  # 版次递增，最后一个即最新
                    elif r["版次"] == revision:
                        return r
        if found is not None:
            return found
        raise ServiceError(f"未知授权：{license_id} 版次 {revision}", "未找到", 404)

    # ---------------- 8. 商品与生产状态 ---------------- #
    def register_product(self, name: str, license_id: str, supplier_id: str,
                         idem_key: str | None = None, *,
                         revision: int | None = None) -> dict:
        lic = self.license_revision(license_id, revision)
        self._get(self.suppliers, supplier_id, "供应商")
        if lic["状态"] == "被替代":
            raise ServiceError(
                f"授权 {license_id} 第 {lic['版次']} 版已被替代，新商品须挂最新版次"
                "（既有商品仍钉住原授权版次）", "授权已更新")
        if "商品生产" not in lic["范围"]:
            raise ServiceError("授权范围不含商品生产，不能登记商品", "授权范围越界")
        return self._record_event("商品登记", {
            "名称": name, "授权": license_id, "版次": lic["版次"],
            "供应商": supplier_id,
        }, idem_key)

    def _apply_product(self, p: dict) -> dict:
        pid = self._new_id("prod")
        lic = self.license_revision(p["授权"], p.get("版次"))
        self.products[pid] = {
            "id": pid, "名称": p["名称"], "授权": p["授权"],
            "授权版次": lic["版次"], "设计版本": lic["版本"],
            "供应商": p["供应商"], "生产状态": "待下单",
            "状态历史": [{"状态": "待下单", "于": today()}],
            "数量": {"在制": 0, "已售": 0},
        }
        return {"商品": pid, "生产状态": "待下单", "授权版次": lic["版次"]}

    def advance_production(self, product_id: str, state: str,
                           idem_key: str | None = None) -> dict:
        self._check_enum("商品生产状态", state, "商品生产状态")
        return self._record_event("生产推进", {
            "商品": product_id, "状态": state,
        }, idem_key)

    def _apply_production(self, p: dict) -> dict:
        prod = self._get(self.products, p["商品"], "商品")
        prod["生产状态"] = p["状态"]
        prod["状态历史"].append({"状态": p["状态"], "于": today()})
        return {"商品": prod["id"], "生产状态": p["状态"]}

    # ---------------- 9. 供应商水印文件（限次限期） ---------------- #
    def register_supplier(self, name: str) -> dict:
        sid = self._new_id("supplier")
        with self._lock:
            self.suppliers[sid] = {"id": sid, "名称": name}
            self._append_raw_event("供应商登记", {"供应商": sid, "名称": name}, None)
        return self.suppliers[sid]

    def grant_watermarked_file(self, product_id: str, max_downloads: int,
                               expires_on: str, idem_key: str | None = None) -> dict:
        """只向商品对应供应商发放带水印文件，限定下载次数与到期日。"""
        prod = self._get(self.products, product_id, "商品")
        return self._record_event("文件发放", {
            "商品": product_id, "供应商": prod["供应商"],
            "上限": max_downloads, "到期日": expires_on,
        }, idem_key)

    def _apply_file_grant(self, p: dict) -> dict:
        prod = self.products[p["商品"]]
        gid = self._new_id("file")
        self.file_grants[gid] = {
            "id": gid, "商品": p["商品"], "供应商": prod["供应商"],
            "水印": True, "上限": p["上限"], "已下载": 0,
            "到期日": p["到期日"], "状态": "有效",
        }
        return {"文件授权": gid, "上限": p["上限"], "到期日": p["到期日"]}

    def download_file(self, grant_id: str, supplier_id: str,
                      idem_key: str | None = None) -> dict:
        self._get(self.file_grants, grant_id, "文件授权")
        return self._record_event("文件下载", {
            "文件授权": grant_id, "供应商": supplier_id, "日期": today(),
        }, idem_key)

    def _apply_file_download(self, p: dict) -> dict:
        g = self.file_grants[p["文件授权"]]
        if g["供应商"] != p["供应商"]:
            raise ServiceError("文件仅对订单所属供应商开放", "越权访问", 403)
        if p["日期"] > g["到期日"]:
            g["状态"] = "已过期"
            raise ServiceError("文件授权已到期", "授权过期", 403)
        if g["已下载"] >= g["上限"]:
            raise ServiceError("下载次数已达上限", "次数超限", 429)
        g["已下载"] += 1
        self.file_downloads.append({
            "文件授权": g["id"], "供应商": p["供应商"], "日期": p["日期"],
            "第几次": g["已下载"],
        })
        return {"文件授权": g["id"], "已下载": g["已下载"], "上限": g["上限"],
                "水印": True}

    # ---------------- 10. 结算分配 ---------------- #
    def generate_settlement(self, product_id: str, period: str, amount: float,
                            idem_key: str | None = None) -> dict:
        """按最终确认的贡献比例生成可追溯分配；同一结算周期幂等。"""
        prod = self._get(self.products, product_id, "商品")
        return self._record_event("结算生成", {
            "商品": product_id, "周期": period, "金额": amount,
            "授权版次": prod["授权版次"],
        }, idem_key)

    def _apply_settlement(self, p: dict) -> dict:
        prod = self.products[p["商品"]]
        claim = self.get_ownership(prod["设计版本"])
        shares = claim["比例"]
        allocation = {}
        used = 0.0
        # 先按比例乘，最后一人兜底尾差，保证分配合计精确等于金额
        items = sorted(shares.items())
        for i, (person_id, share) in enumerate(items):
            if i == len(items) - 1:
                part = round(p["金额"] - used, 2)
            else:
                part = round(p["金额"] * share, 2)
                used += part
            allocation[person_id] = {
                "比例": share, "金额": part,
                "姓名": self.contributors[person_id]["姓名"],
            }
        sid = self._new_id("set")
        self.settlements[sid] = {
            "id": sid, "商品": p["商品"], "周期": p["周期"],
            "金额": p["金额"], "授权版次": p["授权版次"],
            "权属确认": claim["id"], "分配": allocation,
            "生成于": today(),
        }
        return {"结算": sid, "周期": p["周期"], "金额": p["金额"],
                "分配": allocation}

    # ---------------- 11. 撤回 / 争议（不删记录，分级影响） ---------------- #
    def withdraw_contribution(self, contribution_id: str, reason: str,
                              idem_key: str | None = None) -> dict:
        return self._record_event("撤回", {
            "贡献": contribution_id, "原因": reason,
        }, idem_key)

    def dispute_contribution(self, contribution_id: str, case_ref: str,
                             idem_key: str | None = None) -> dict:
        return self._record_event("争议", {
            "贡献": contribution_id, "案件": case_ref,
        }, idem_key)

    def _apply_withdraw(self, p: dict) -> dict:
        c = self._get(self.contributions, p["贡献"], "贡献")
        for purpose in c["用途授权"]:
            if c["用途授权"][purpose] not in ("已撤回",):
                c["用途授权"][purpose] = "已撤回"
        c["授权状态"] = "已撤回"
        c["可商用"] = False
        c["可用于设计"] = False
        return {"贡献": c["id"], "授权状态": "已撤回", "原因": p["原因"]}

    def _apply_dispute(self, p: dict) -> dict:
        c = self._get(self.contributions, p["贡献"], "贡献")
        c["授权状态"] = "争议中"
        c["可用于设计"] = False
        return {"贡献": c["id"], "授权状态": "争议中", "案件": p["案件"]}

    def impact_analysis(self, contribution_id: str) -> dict:
        """找出受影响的版本、授权、商品，并按生产状态分桶（不删除任何记录）。"""
        c = self._get(self.contributions, contribution_id, "贡献")
        affected_versions = [vid for vid in self.versions
                             if contribution_id in self.provenance(vid)["素材闭包"]]
        licenses = [l for v in affected_versions
                    for l in self.license_versions.get(v, [])]
        products = [self.products[pid] for pid in self.products
                    if self.products[pid]["设计版本"] in affected_versions]
        buckets: dict[str, list] = defaultdict(list)
        for prod in products:
            buckets[prod["生产状态"]].append(
                {"商品": prod["id"], "名称": prod["名称"],
                 "授权版次": prod["授权版次"]})
        not_started = buckets["待下单"] + buckets["尚未生产"] + buckets["订单取消"]
        in_making = buckets["在制中"] + buckets["生产暂停"]
        sold = buckets["已售出"] + buckets["已完成未售"]
        return {
            "贡献": contribution_id, "状态": c["授权状态"],
            "受影响版本": affected_versions,
            "受影响授权": [{"授权": l["id"], "版次": l["版次"], "状态": l["状态"]}
                          for l in licenses],
            "尚未生产": not_started,
            "在制": in_making,
            "已售出或已完成": sold,
            "处置原则": [
                "尚未生产：暂停下单，等待替换素材或授权补正",
                "在制：暂停产线，评估半成品与已备料损失",
                "已售出/已完成：不追回已售商品，按约定结算并留存账目，后续批次停用",
            ],
        }

    # ---------------- 对账与快照 ---------------- #
    def verify_consistency(self) -> dict:
        """核对账目与来源图一致：事件全部可重放、引用完整、结算可追溯。"""
        problems = []
        with self._lock:
            for v in self.versions.values():
                for parent in v["父版本"]:
                    if parent not in self.versions:
                        problems.append(f"版本 {v['id']} 父版本悬空：{parent}")
                for cid in v["所用素材"]:
                    if cid not in self.contributions:
                        problems.append(f"版本 {v['id']} 素材悬空：{cid}")
            for pid, prod in self.products.items():
                if prod["设计版本"] not in self.versions:
                    problems.append(f"商品 {pid} 设计版本悬空")
                if prod["设计版本"] not in self.ownership_claims:
                    problems.append(f"商品 {pid} 缺少权属确认")
            for sid, st in self.settlements.items():
                prod = self.products[st["商品"]]
                claim = self.ownership_claims.get(prod["设计版本"])
                if not claim or claim["id"] != st["权属确认"]:
                    problems.append(f"结算 {sid} 与最终权属确认不一致")
                total = round(sum(a["金额"] for a in st["分配"].values()), 2)
                if abs(total - st["金额"]) > 0.01:
                    problems.append(f"结算 {sid} 分配合计 {total} != {st['金额']}")
            # 幂等键唯一
            if len(self.idempotency) != len(set(self.idempotency.values())):
                problems.append("幂等键映射出现重复事件")
        return {"一致": not problems, "问题": problems,
                "事件数": len(self.events),
                "版本数": len(self.versions),
                "商品数": len(self.products)}

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps({
                "简报": self.briefs, "贡献者": self.contributors,
                "贡献": self.contributions, "生成批次": self.generation_batches,
                "版本": self.versions, "权属": self.ownership_claims,
                "样品": self.samples,
                "授权": [l for revs in self.license_versions.values() for l in revs],
                "商品": self.products, "供应商": self.suppliers,
                "文件授权": self.file_grants, "下载记录": self.file_downloads,
                "结算": self.settlements, "事件": self.events,
            }, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def build_service() -> CreativeRightsService:
    return CreativeRightsService(load_domain())


class Handler(BaseHTTPRequestHandler):
    service = build_service()

    def _send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            raise ServiceError("请求体不是合法 JSON", "格式错误", 400)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, health())
        elif path == "/domain":
            self._send(200, self.service.domain)
        elif path == "/events":
            self._send(200, {"事件": self.service.events})
        elif path == "/verify":
            self._send(200, self.service.verify_consistency())
        elif path.startswith("/provenance/"):
            vid = path.rsplit("/", 1)[-1]
            try:
                self._send(200, self.service.provenance(vid))
            except ServiceError as e:
                self._send(e.status, {"错误": e.message, "代码": e.code})
        elif path.startswith("/impact/"):
            cid = path.rsplit("/", 1)[-1]
            try:
                self._send(200, self.service.impact_analysis(cid))
            except ServiceError as e:
                self._send(e.status, {"错误": e.message, "代码": e.code})
        else:
            self._send(404, {"错误": "未知路径", "代码": "未找到"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        idem = body.pop("幂等键", None)
        routes = {
            "/briefs": lambda: self.service.create_brief(
                body["标题"], body.get("摘要", ""), idem),
            "/contributors": lambda: self.service.register_contributor(
                body["姓名"], body.get("未成年", False)),
            "/contributions": lambda: self.service.submit_contribution(
                body["简报"], body["贡献者"], body["类型"], body["标题"],
                body.get("申报来源", []), idem),
            "/guardian": lambda: self.service.guardian_confirm(
                body["贡献者"], body["监护人"], idem),
            "/grants": lambda: self.service.grant_purpose(
                body["贡献"], body["用途"], body["状态"], idem),
            "/batches": lambda: self.service.register_generation_batch(
                body["简报"], body["提示词"], body["模型"], body.get("输入贡献", [])),
            "/callbacks": lambda: self.service.generation_callback(
                body["批次"], body["成功"], body.get("产出"), idem,
                body.get("说明", "")),
            "/versions": lambda: self.service.merge_version(
                body["简报"], body["类型"], body["父版本"],
                body.get("所用素材", []), body.get("说明", ""), idem),
            "/ownership": lambda: self.service.confirm_ownership(
                body["版本"], body["比例"], idem),
            "/samples": lambda: self.service.submit_sample(
                body["版本"], body["供应商"], body["样品"], idem),
            "/sample-reviews": lambda: self.service.review_sample(
                body["样品"], body["通过"], body.get("原因", ""), idem),
            "/licenses": lambda: self.service.issue_license(
                body["版本"], body["范围"], idem),
            "/suppliers": lambda: self.service.register_supplier(body["名称"]),
            "/products": lambda: self.service.register_product(
                body["名称"], body["授权"], body["供应商"], idem,
                revision=body.get("版次")),
            "/production": lambda: self.service.advance_production(
                body["商品"], body["状态"], idem),
            "/files": lambda: self.service.grant_watermarked_file(
                body["商品"], body["上限"], body["到期日"], idem),
            "/downloads": lambda: self.service.download_file(
                body["文件授权"], body["供应商"], idem),
            "/settlements": lambda: self.service.generate_settlement(
                body["商品"], body["周期"], body["金额"], idem),
            "/withdraw": lambda: self.service.withdraw_contribution(
                body["贡献"], body["原因"], idem),
            "/dispute": lambda: self.service.dispute_contribution(
                body["贡献"], body["案件"], idem),
        }
        handler = routes.get(path)
        if handler is None:
            self._send(404, {"错误": "未知路径", "代码": "未找到"})
            return
        try:
            self._send(200, {"ok": True, "数据": handler()})
        except ServiceError as e:
            self._send(e.status, {"ok": False, "错误": e.message, "代码": e.code})
        except KeyError as e:
            self._send(400, {"ok": False, "错误": f"缺少字段：{e}", "代码": "字段缺失"})

    def log_message(self, *_args):
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="文创设计权利转换")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        domain = load_domain()
        svc = CreativeRightsService(domain)
        result = svc.verify_consistency()
        print("基础检查通过：", json.dumps(domain, ensure_ascii=False))
        print("一致性检查：", json.dumps(result, ensure_ascii=False))
    else:
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
