from __future__ import annotations

from urllib.parse import parse_qs, urlparse
import queue_performance as qp


def get(handler, app):
    parsed = urlparse(handler.path)
    if parsed.path == '/api/performance-state':
        if not handler.require_admin(): return True
        handler.json_response(qp.snapshot(app))
        return True
    if parsed.path == '/api/error-explain':
        query = parse_qs(parsed.query)
        raw = query.get('error', [''])[0][:500]
        handler.json_response({'message': qp.friendly_error(raw)})
        return True
    return False


def post(handler, app):
    parsed = urlparse(handler.path)
    if parsed.path != '/api/performance-telemetry':
        return False
    payload = handler.read_authorized_json(100_000)
    if payload is None:
        return True
    qp.update_connector_telemetry(payload)
    handler.json_response({'ok': True})
    return True
