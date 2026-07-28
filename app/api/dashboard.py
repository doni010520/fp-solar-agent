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
from app.services import contact_updater

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


@router.post("/run-followups")
async def run_followups(token: str = Query(...)) -> JSONResponse:
    """Executa manualmente uma sweep de follow-ups.
    Também roda automaticamente a cada 15min via scheduler interno."""
    await _check_token(token)
    from app.services import follow_up_service
    result = await follow_up_service.run_batch()
    return JSONResponse(result)


@router.get("/followups.json")
async def followups_json(token: str = Query(...), limit: int = Query(30, ge=1, le=200)) -> JSONResponse:
    """Últimos N follow-ups enviados."""
    await _check_token(token)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text("""
          SELECT f.tentativa, f.mensagem, f.enviado_em, f.sucesso, f.erro,
                 l.telefone, coalesce(l.full_name, l.push_name, '(sem nome)') as nome,
                 l.status_funil_vendas, l.ia_on_off
          FROM follow_ups f JOIN leads l ON l.id = f.lead_id
          ORDER BY f.enviado_em DESC LIMIT :limit
        """), {"limit": limit})).mappings().all()
    return JSONResponse({"follow_ups": [
        {**dict(r), "enviado_em": r["enviado_em"].isoformat()} for r in rows
    ]})


@router.get("/logs")
async def logs_tail(
    token: str = Query(...),
    n: int = Query(200, ge=1, le=2000),
    contains: str | None = Query(None),
) -> JSONResponse:
    """Últimas N linhas do ring buffer de logs (texto plano)."""
    await _check_token(token)
    from app.core.log_buffer import tail
    return JSONResponse({"lines": tail(n, contains)})


@router.get("/conversas.json")
async def conversas_json(
    token: str = Query(...),
    limit: int = Query(20, ge=1, le=200),
) -> JSONResponse:
    await _check_token(token)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Lead)
            .order_by(Lead.updated_at.desc())
            .limit(limit)
        )).scalars().all()
        out = []
        for l in rows:
            # Conta mensagens
            n_msgs = (await s.execute(
                select(func.count(Message.id)).where(Message.lead_id == l.id)
            )).scalar() or 0
            # Última mensagem do usuário
            last_user = (await s.execute(
                select(Message)
                .where(Message.lead_id == l.id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            out.append({
                "telefone": l.telefone,
                "nome": l.full_name or l.push_name or "(sem nome)",
                "ia": l.ia_on_off,
                "status": l.status_funil_vendas,
                "tipo_projeto": l.tipo_projeto,
                "cidade": l.cidade,
                "ultimo_contato": l.ultimo_contato.isoformat() if l.ultimo_contato else None,
                "updated_at": l.updated_at.isoformat(),
                "total_mensagens": n_msgs,
                "ultima_msg_user": (last_user.content[:140] if last_user else None),
            })
    return JSONResponse({"conversas": out})


@router.post("/lead/{phone}/ia")
async def toggle_ia(phone: str, status: str = Query(..., regex="^(ON|OFF)$"), token: str = Query(...)) -> JSONResponse:
    await _check_token(token)
    if status == "OFF":
        await contact_updater.disable_ia(phone, "atendimento_humano")
    else:
        await contact_updater.update_lead(phone, ia_on_off="ON", status_funil_vendas="em_qualificacao")
    return JSONResponse({"ok": True, "phone": phone, "ia": status})


@router.get("/conversas", response_class=HTMLResponse)
async def conversas_html(token: str = Query(...), limit: int = Query(20, ge=1, le=200)) -> HTMLResponse:
    await _check_token(token)
    html = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FP Solar — Conversas</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #0b0f17; color: #e6e9ef; margin: 0; padding: 24px; }}
h1 {{ margin: 0 0 6px; font-size: 22px; }}
.sub {{ color: #8a93a6; font-size: 13px; margin-bottom: 24px; }}
.toolbar {{ display: flex; gap: 12px; margin-bottom: 16px; align-items: center; }}
.toolbar input {{ background: #151a26; border: 1px solid #232a3a; color: #e6e9ef; padding: 8px 12px; border-radius: 8px; font-size: 13px; flex: 1; max-width: 360px; }}
.toolbar button {{ background: #1f2538; border: 1px solid #232a3a; color: #e6e9ef; padding: 8px 14px; border-radius: 8px; font-size: 13px; cursor: pointer; }}
.toolbar button:hover {{ background: #2a3149; }}
.card {{ background: #151a26; border: 1px solid #232a3a; border-radius: 12px; padding: 16px; margin-bottom: 12px;
         display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; }}
.who {{ display: flex; flex-direction: column; gap: 4px; min-width: 0; }}
.who .name {{ font-weight: 600; font-size: 15px; }}
.who .phone {{ color: #8a93a6; font-size: 12px; font-variant-numeric: tabular-nums; }}
.meta {{ color: #94a3b8; font-size: 12px; display: flex; gap: 12px; flex-wrap: wrap; margin-top: 2px; }}
.meta span {{ white-space: nowrap; }}
.lastmsg {{ color: #cbd5e1; font-size: 13px; margin-top: 8px; padding: 8px 10px; background: #0f1421; border-radius: 6px;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
.controls {{ display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }}
.toggle {{ position: relative; display: inline-block; width: 56px; height: 30px; }}
.toggle input {{ opacity: 0; width: 0; height: 0; }}
.slider {{ position: absolute; cursor: pointer; inset: 0; background: #4a1d0c; border-radius: 999px; transition: .15s; }}
.slider::before {{ content: ""; position: absolute; height: 22px; width: 22px; left: 4px; top: 4px;
                   background: #fb923c; border-radius: 50%; transition: .15s; }}
.toggle input:checked + .slider {{ background: #103e2c; }}
.toggle input:checked + .slider::before {{ transform: translateX(26px); background: #4ade80; }}
.toggle.loading .slider {{ opacity: 0.4; pointer-events: none; }}
.ia-label {{ font-size: 11px; color: #8a93a6; text-transform: uppercase; letter-spacing: 0.5px; }}
.status-badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
                  background: #1f2538; color: #94a3b8; }}
.status-badge.novo {{ background: #1e3a5f; color: #93c5fd; }}
.status-badge.transferido_para_time {{ background: #103e2c; color: #4ade80; }}
.status-badge.atendimento_humano {{ background: #4a1d0c; color: #fb923c; }}
.empty {{ color: #6c7588; padding: 40px; text-align: center; }}
@media (max-width: 640px) {{
  body {{ padding: 12px; }}
  .card {{ grid-template-columns: 1fr; }}
  .controls {{ align-items: flex-start; flex-direction: row; }}
}}
</style>
</head><body>
<h1>FP Solar — Conversas</h1>
<div class="sub">Painel de controle de IA por contato</div>

<div class="toolbar">
  <input id="filter" placeholder="Filtrar por nome ou telefone…">
  <button onclick="loadConversas()">🔄 Atualizar</button>
</div>

<div id="container"><div class="empty">Carregando…</div></div>

<script>
const TOKEN = {repr(token)};
const LIMIT = {limit};

async function loadConversas() {{
  const r = await fetch(`/admin/conversas.json?token=${{TOKEN}}&limit=${{LIMIT}}`);
  const data = await r.json();
  render(data.conversas);
}}

function render(list) {{
  const container = document.getElementById("container");
  if (!list || list.length === 0) {{
    container.innerHTML = '<div class="empty">Nenhuma conversa ainda</div>';
    return;
  }}
  container.innerHTML = list.map(c => {{
    const lastTs = c.ultimo_contato ? new Date(c.ultimo_contato).toLocaleString('pt-BR') : '—';
    const status = c.status || 'novo';
    const tipo = c.tipo_projeto || '—';
    const cidade = c.cidade || '—';
    const lastMsg = c.ultima_msg_user ? `<div class="lastmsg">"${{escapeHtml(c.ultima_msg_user)}}"</div>` : '';
    return `
      <div class="card" data-phone="${{c.telefone}}" data-name="${{escapeHtml(c.nome).toLowerCase()}}">
        <div class="who">
          <div class="name">${{escapeHtml(c.nome)}}</div>
          <div class="phone">📱 ${{c.telefone}}</div>
          <div class="meta">
            <span class="status-badge ${{status}}">${{status}}</span>
            <span>🏠 ${{tipo}}</span>
            <span>📍 ${{cidade}}</span>
            <span>💬 ${{c.total_mensagens}} msgs</span>
            <span>🕐 ${{lastTs}}</span>
          </div>
          ${{lastMsg}}
        </div>
        <div class="controls">
          <span class="ia-label">IA ${{c.ia}}</span>
          <label class="toggle">
            <input type="checkbox" ${{c.ia === 'ON' ? 'checked' : ''}} onchange="toggleIA('${{c.telefone}}', this)">
            <span class="slider"></span>
          </label>
        </div>
      </div>`;
  }}).join('');
}}

async function toggleIA(phone, checkbox) {{
  const newStatus = checkbox.checked ? 'ON' : 'OFF';
  const label = checkbox.parentElement;
  label.classList.add('loading');
  try {{
    const r = await fetch(`/admin/lead/${{phone}}/ia?status=${{newStatus}}&token=${{TOKEN}}`, {{method: 'POST'}});
    if (!r.ok) throw new Error('falha');
    // Atualiza label local
    const card = checkbox.closest('.card');
    card.querySelector('.ia-label').textContent = `IA ${{newStatus}}`;
  }} catch (e) {{
    alert('Erro ao alternar IA: ' + e.message);
    checkbox.checked = !checkbox.checked;
  }} finally {{
    label.classList.remove('loading');
  }}
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

document.getElementById('filter').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.card').forEach(card => {{
    const name = card.dataset.name || '';
    const phone = card.dataset.phone || '';
    card.style.display = (name.includes(q) || phone.includes(q)) ? '' : 'none';
  }});
}});

loadConversas();
setInterval(loadConversas, 30000);
</script>
</body></html>
"""
    return HTMLResponse(html)


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
<div class="sub">Painel de status · <a href="/admin/conversas?token={settings.admin_token}" style="color:#4ade80">→ Conversas (toggle IA)</a></div>

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
