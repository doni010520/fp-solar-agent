"""
Dashboard de status. Acesso protegido por ADMIN_TOKEN (env var).

URL: /admin/dashboard?token=XXX
JSON: /admin/dashboard.json?token=XXX
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text, select, func

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, engine
from app.models import Lead, Message, Notification
from app.services.uazapi import uazapi

settings = get_settings()
router = APIRouter(prefix="/admin", tags=["admin"])


async def _check_token(token: str | None) -> None:
    expected = getattr(settings, "admin_token", None) or ""
    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN não configurado no servidor")
    if token != expected:
        raise HTTPException(401, "token inválido")


async def _collect_stats() -> dict:
    """Coleta métricas atuais — chamado pelo HTML e pelo JSON."""
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_1h = now - timedelta(hours=1)

    async with AsyncSessionLocal() as s:
        # Counts globais
        total_leads = (await s.execute(select(func.count(Lead.id)))).scalar() or 0
        leads_24h = (await s.execute(select(func.count(Lead.id)).where(Lead.created_at >= last_24h))).scalar() or 0
        leads_on = (await s.execute(select(func.count(Lead.id)).where(Lead.ia_on_off == "ON"))).scalar() or 0
        leads_off = (await s.execute(select(func.count(Lead.id)).where(Lead.ia_on_off == "OFF"))).scalar() or 0

        msgs_24h = (await s.execute(select(func.count(Message.id)).where(Message.created_at >= last_24h))).scalar() or 0
        msgs_1h = (await s.execute(select(func.count(Message.id)).where(Message.created_at >= last_1h))).scalar() or 0

        notif_24h = (await s.execute(select(func.count(Notification.id)).where(Notification.enviado_em >= last_24h))).scalar() or 0
        notif_qual_total = (await s.execute(select(func.count(Notification.id)).where(Notification.tipo == "qualified_lead"))).scalar() or 0
        notif_hum_total = (await s.execute(select(func.count(Notification.id)).where(Notification.tipo == "human_request"))).scalar() or 0

        # Funil
        funil = {}
        rows = (await s.execute(
            select(Lead.status_funil_vendas, func.count(Lead.id))
            .group_by(Lead.status_funil_vendas)
        )).all()
        for status, n in rows:
            funil[status or "(null)"] = n

        # Leads recentes (top 20)
        recent_leads_rows = (await s.execute(
            select(Lead).order_by(Lead.updated_at.desc()).limit(20)
        )).scalars().all()
        recent_leads = [
            {
                "telefone": l.telefone,
                "nome": l.full_name or l.push_name or "(sem nome)",
                "ia": l.ia_on_off,
                "status": l.status_funil_vendas,
                "tipo_projeto": l.tipo_projeto or "—",
                "cidade": l.cidade or "—",
                "ultimo_contato": l.ultimo_contato.isoformat() if l.ultimo_contato else None,
                "updated_at": l.updated_at.isoformat(),
            }
            for l in recent_leads_rows
        ]

        # Notificações recentes (top 10)
        recent_notif_rows = (await s.execute(
            select(Notification, Lead.telefone, Lead.full_name, Lead.push_name)
            .join(Lead, Notification.lead_id == Lead.id)
            .order_by(Notification.enviado_em.desc())
            .limit(10)
        )).all()
        recent_notif = [
            {
                "tipo": n.tipo,
                "sucesso": n.sucesso,
                "telefone": tel,
                "nome": full or push or "(sem nome)",
                "enviado_em": n.enviado_em.isoformat(),
                "erro": n.erro,
            }
            for n, tel, full, push in recent_notif_rows
        ]

    # Health dos serviços externos
    health: dict = {"app": "ok"}
    try:
        async with engine.connect() as c:
            await c.execute(text("select 1"))
        health["db"] = "ok"
    except Exception as e:
        health["db"] = f"error: {type(e).__name__}"
    try:
        s = await uazapi.get_instance_status()
        health["uazapi"] = s.get("instance", {}).get("status") if s else "unreachable"
    except Exception as e:
        health["uazapi"] = f"error: {type(e).__name__}"

    return {
        "now": now.isoformat(),
        "health": health,
        "totals": {
            "leads": total_leads,
            "leads_24h": leads_24h,
            "leads_ia_on": leads_on,
            "leads_ia_off": leads_off,
            "msgs_24h": msgs_24h,
            "msgs_1h": msgs_1h,
            "notif_24h": notif_24h,
            "notif_qual_total": notif_qual_total,
            "notif_hum_total": notif_hum_total,
        },
        "funil": funil,
        "leads_recentes": recent_leads,
        "notificacoes_recentes": recent_notif,
    }


@router.get("/dashboard.json")
async def dashboard_json(token: str = Query(...)) -> JSONResponse:
    await _check_token(token)
    return JSONResponse(await _collect_stats())


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(token: str = Query(...)) -> HTMLResponse:
    await _check_token(token)
    stats = await _collect_stats()

    def badge(value, ok="ok") -> str:
        cls = "ok" if value == ok else ("warn" if str(value).startswith("error") or value == "unreachable" else "neutral")
        return f'<span class="badge {cls}">{value}</span>'

    rows_leads = "\n".join(
        f"""
        <tr>
          <td>{l['telefone']}</td>
          <td>{l['nome']}</td>
          <td>{badge(l['ia'], 'ON')}</td>
          <td>{l['status']}</td>
          <td>{l['tipo_projeto']}</td>
          <td>{l['cidade']}</td>
          <td class="ts">{l['ultimo_contato'] or '—'}</td>
        </tr>"""
        for l in stats["leads_recentes"]
    )
    rows_notif = "\n".join(
        f"""
        <tr>
          <td>{'⭐' if n['tipo']=='qualified_lead' else '🚨'} {n['tipo']}</td>
          <td>{n['nome']}</td>
          <td>{n['telefone']}</td>
          <td>{badge('ok' if n['sucesso'] else 'falha', 'ok')}</td>
          <td class="ts">{n['enviado_em']}</td>
        </tr>"""
        for n in stats["notificacoes_recentes"]
    )
    funil_rows = "\n".join(
        f"<li><strong>{k}</strong>: {v}</li>" for k, v in sorted(stats["funil"].items(), key=lambda x: -x[1])
    )

    t = stats["totals"]
    h = stats["health"]
    html = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FP Solar — Lara | Status</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #0b0f17; color: #e6e9ef; margin: 0; padding: 24px; }}
h1 {{ margin: 0 0 6px; font-size: 22px; }}
.sub {{ color: #8a93a6; font-size: 13px; margin-bottom: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 12px; margin-bottom: 24px; }}
.card {{ background: #151a26; border: 1px solid #232a3a; border-radius: 12px; padding: 16px; }}
.card .label {{ font-size: 12px; color: #8a93a6; text-transform: uppercase; letter-spacing: 0.5px; }}
.card .value {{ font-size: 28px; font-weight: 600; margin-top: 4px; }}
.card .hint {{ font-size: 11px; color: #6c7588; margin-top: 4px; }}
.section {{ background: #151a26; border: 1px solid #232a3a; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
.section h2 {{ margin: 0 0 12px; font-size: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #232a3a; }}
th {{ color: #8a93a6; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
tr:last-child td {{ border-bottom: 0; }}
.ts {{ color: #8a93a6; font-variant-numeric: tabular-nums; font-size: 12px; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
.badge.ok {{ background: #103e2c; color: #4ade80; }}
.badge.warn {{ background: #4a1d0c; color: #fb923c; }}
.badge.neutral {{ background: #1f2538; color: #94a3b8; }}
.health {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
.health .item {{ display: flex; align-items: center; gap: 6px; }}
ul.funil {{ list-style: none; padding: 0; margin: 0; columns: 2; }}
ul.funil li {{ padding: 4px 0; }}
.refresh {{ float: right; font-size: 12px; color: #8a93a6; }}
</style>
</head><body>
<h1>FP Solar — Lara <span class="refresh">auto-refresh 30s · {stats['now']}</span></h1>
<div class="sub">Painel de status da aplicação</div>

<div class="section">
  <h2>Health</h2>
  <div class="health">
    <div class="item">app: {badge(h['app'])}</div>
    <div class="item">db: {badge(h['db'])}</div>
    <div class="item">uazapi: {badge(h['uazapi'], 'connected')}</div>
  </div>
</div>

<div class="grid">
  <div class="card"><div class="label">Total de leads</div><div class="value">{t['leads']}</div><div class="hint">{t['leads_24h']} nas últimas 24h</div></div>
  <div class="card"><div class="label">IA ligada</div><div class="value">{t['leads_ia_on']}</div><div class="hint">{t['leads_ia_off']} desligadas</div></div>
  <div class="card"><div class="label">Mensagens 24h</div><div class="value">{t['msgs_24h']}</div><div class="hint">{t['msgs_1h']} na última hora</div></div>
  <div class="card"><div class="label">Notificações 24h</div><div class="value">{t['notif_24h']}</div><div class="hint">⭐ {t['notif_qual_total']} · 🚨 {t['notif_hum_total']} no total</div></div>
</div>

<div class="section">
  <h2>Funil</h2>
  <ul class="funil">{funil_rows}</ul>
</div>

<div class="section">
  <h2>Leads recentes</h2>
  <table>
    <thead><tr><th>Telefone</th><th>Nome</th><th>IA</th><th>Status</th><th>Tipo</th><th>Cidade</th><th>Último contato</th></tr></thead>
    <tbody>{rows_leads}</tbody>
  </table>
</div>

<div class="section">
  <h2>Notificações recentes</h2>
  <table>
    <thead><tr><th>Tipo</th><th>Cliente</th><th>Telefone</th><th>Sucesso</th><th>Enviado em</th></tr></thead>
    <tbody>{rows_notif or '<tr><td colspan=5 class=ts>nenhuma notificação ainda</td></tr>'}</tbody>
  </table>
</div>

<script>setTimeout(() => location.reload(), 30000);</script>
</body></html>
"""
    return HTMLResponse(html)
