"""HTTP API 层：把 JSON 请求映射到 RightsEngine。

所有写接口接受可选的幂等键（请求头 Idempotency-Key 或 body.idem）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .engine import DomainError, RightsEngine, load_domain

# (method, path, engine 方法, 位置参数段名, 允许的 body 键)
ROUTES = [
    ("POST", "/briefs", "create_brief", [], ["title", "summary"]),
    ("POST", "/contributors", "register_contributor", [],
     ["ctype", "name", "guardian"]),
    ("POST", "/materials", "contribute_material", [],
     ["brief_id", "contributor_id", "kind", "title", "file_ref", "note"]),
    ("POST", "/materials/{}/confirm", "confirm_scope", ["material_id"],
     ["scope", "action", "by"]),
    ("POST", "/batches", "create_batch", [],
     ["brief_id", "prompt", "source_material_ids", "model", "operator"]),
    ("POST", "/designs", "create_design", [],
     ["brief_id", "title", "created_by", "material_edges",
      "parent_edges", "batch_edges"]),
    ("GET", "/designs/{}/provenance", "provenance", ["design_id"], []),
    ("POST", "/designs/{}/review-request", "request_review", ["design_id"], []),
    ("POST", "/designs/{}/review", "decide_review", ["design_id"],
     ["scopes", "reviewer"]),
    ("POST", "/samples", "create_sample", [],
     ["design_id", "supplier_id", "spec", "product_id"]),
    ("POST", "/samples/{}/rounds", "submit_sample_round", ["sample_id"],
     ["file_ref", "note", "callback_id"]),
    ("POST", "/samples/{}/verify", "verify_sample", ["sample_id"],
     ["accepted", "reviewer", "reason"]),
    ("POST", "/samples/{}/seal", "seal_sample", ["sample_id"], []),
    ("POST", "/products", "create_product", [], ["title", "design_shares"]),
    ("POST", "/products/{}/activate", "activate_product", ["product_id"], []),
    ("POST", "/production-batches", "create_production_batch", [],
     ["product_id", "qty", "status"]),
    ("POST", "/production-batches/{}/update", "update_production_batch",
     ["batch_id"], ["status"]),
    ("POST", "/sales", "record_sale", [],
     ["product_id", "qty", "channel", "order_ref"]),
    ("POST", "/products/{}/licenses", "issue_license", ["product_id"],
     ["scopes", "channels", "terms"]),
    ("POST", "/products/{}/licenses/update", "update_license", ["product_id"],
     ["action", "scopes", "channels", "terms"]),
    ("POST", "/products/{}/revise", "revise_product_design", ["product_id"],
     ["design_shares", "scopes", "reason"]),
    ("POST", "/orders", "create_order", [],
     ["product_id", "supplier_id", "qty"]),
    ("POST", "/orders/{}/grants", "issue_download_grant", ["order_id"],
     ["file_variant", "max_downloads", "expires_at"]),
    ("POST", "/grants/{}/fetch", "fetch_file", ["grant_id"], []),
    ("GET", "/materials/{}/impact", "impact_analysis", ["material_id"], []),
    ("POST", "/materials/{}/withdraw", "withdraw_material", ["material_id"],
     ["reason", "scope"]),
    ("POST", "/materials/{}/dispute", "open_dispute", ["material_id"],
     ["reason"]),
    ("POST", "/materials/{}/dispute/resolve", "resolve_dispute",
     ["material_id"], ["resolution"]),
    ("POST", "/settlements", "create_settlement_run", [],
     ["product_id", "period", "amount"]),
    ("POST", "/settlements/{}/lock", "lock_settlement", ["run_id"], []),
    ("POST", "/settlements/{}/callback", "payment_callback", ["run_id"],
     ["callback_id"]),
]

REQUIRED = {
    "/briefs": ["title"],
    "/contributors": ["ctype", "name"],
    "/materials": ["brief_id", "contributor_id", "kind", "title"],
    "/materials/{}/confirm": ["scope", "action", "by"],
    "/batches": ["brief_id", "prompt", "source_material_ids", "model", "operator"],
    "/designs": ["brief_id", "title", "created_by"],
    "/designs/{}/review": ["scopes", "reviewer"],
    "/samples": ["design_id", "supplier_id", "spec"],
    "/samples/{}/rounds": ["file_ref"],
    "/samples/{}/verify": ["accepted", "reviewer"],
    "/products": ["title", "design_shares"],
    "/production-batches": ["product_id", "qty"],
    "/production-batches/{}/update": ["status"],
    "/sales": ["product_id", "qty", "channel"],
    "/products/{}/licenses": ["scopes", "channels"],
    "/products/{}/licenses/update": ["action", "scopes"],
    "/products/{}/revise": ["design_shares", "scopes", "reason"],
    "/orders": ["product_id", "supplier_id", "qty"],
    "/orders/{}/grants": ["file_variant", "max_downloads", "expires_at"],
    "/materials/{}/withdraw": ["reason"],
    "/materials/{}/dispute": ["reason"],
    "/materials/{}/dispute/resolve": ["resolution"],
    "/settlements": ["product_id", "period", "amount"],
    "/settlements/{}/callback": ["callback_id"],
}


def _match_route(method: str, path: str):
    parts = path.strip("/").split("/")
    for rmethod, pattern, fn_name, arg_names, body_keys in ROUTES:
        if rmethod != method:
            continue
        pparts = pattern.strip("/").split("/")
        if len(pparts) != len(parts):
            continue
        args, ok = [], True
        for token, actual in zip(pparts, parts):
            if token == "{}":
                args.append(actual)
            elif token != actual:
                ok = False
                break
        if ok:
            return fn_name, arg_names, args, body_keys, pattern
    return None


def make_handler(engine: RightsEngine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RightsConversion/1.0"

        def _send(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send(200, {"status": "ok", "service": "creative-rights-conversion"})
                return
            if path == "/domain":
                self._send(200, engine.domain)
                return
            if path == "/consistency":
                self._send(200, engine.consistency_report())
                return
            if path == "/events":
                self._send(200, {"events": engine.events(
                    query.get("entity_type", [None])[0],
                    query.get("entity_id", [None])[0])})
                return
            match = _match_route("GET", path)
            if not match:
                self._send(404, {"error": "not_found", "path": path})
                return
            fn_name, _arg_names, args, _keys, _pat = match
            self._send(200, getattr(engine, fn_name)(*args))

        def do_POST(self):
            path = urlparse(self.path).path
            match = _match_route("POST", path)
            if not match:
                self._send(404, {"error": "not_found", "path": path})
                return
            fn_name, _arg_names, args, body_keys, pattern = match
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"error": "body_must_be_object"})
                return
            for field in REQUIRED.get(pattern, []):
                if field not in payload or payload[field] in (None, ""):
                    self._send(400, {"error": "missing_field", "field": field})
                    return
            kwargs = {k: payload[k] for k in body_keys if k in payload}
            idem = self.headers.get("Idempotency-Key") or payload.get("idem")
            if idem:
                kwargs["idem"] = str(idem)
            try:
                result = getattr(engine, fn_name)(*args, **kwargs)
            except DomainError as exc:
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
                return
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": "bad_request", "message": str(exc)})
                return
            self._send(200, result)

    return Handler


def serve(port: int = 8000, data_path: str | None = None):
    engine = RightsEngine(domain=load_domain(), data_path=data_path)
    print(f"文创设计权利转换服务监听 :{port}" + (f"，数据文件 {data_path}" if data_path else ""))
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(engine)).serve_forever()
