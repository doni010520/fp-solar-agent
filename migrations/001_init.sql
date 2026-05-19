-- FP Solar Agent — schema inicial
-- Roda manualmente no SQL Editor do Supabase OU via script run_migrations.py

create extension if not exists "pgcrypto";

-- ── leads ────────────────────────────────────────────────────
create table if not exists public.leads (
    id              uuid primary key default gen_random_uuid(),
    telefone        text not null unique,
    full_name       text,
    push_name       text,
    email           text,
    cpf             text,
    data_nascimento date,

    -- Qualificação (campos da Lara)
    tipo_projeto       text,   -- residencial / rural / empresarial
    tipo_telhado       text,   -- colonial / zinco / eternit / laje
    padrao_energia     text,   -- monofasico / bifasico / trifasico
    cidade             text,
    valor_conta_luz    numeric(10,2),
    observacoes        text,

    -- Estado do funil
    status_funil_vendas text default 'novo',          -- novo / em_qualificacao / qualificado / transferido_para_time / atendimento_humano
    etapa_follow_up     text default 'aguardando_primeira_mensagem',
    ia_on_off           text default 'ON',             -- ON / OFF (OFF = humano assumiu)

    ultimo_contato      timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists idx_leads_status on public.leads(status_funil_vendas);
create index if not exists idx_leads_ia on public.leads(ia_on_off);

-- ── messages ─────────────────────────────────────────────────
create table if not exists public.messages (
    id           uuid primary key default gen_random_uuid(),
    lead_id      uuid not null references public.leads(id) on delete cascade,
    role         text not null,                       -- user / assistant / tool / system
    content      text not null,
    tool_name    text,
    tool_args    jsonb,
    message_id_wpp text,                              -- ID do whatsapp (uazapi)
    message_type text default 'text',                 -- text / audio / image / document
    created_at   timestamptz not null default now()
);

create index if not exists idx_messages_lead on public.messages(lead_id, created_at);

-- ── notifications ────────────────────────────────────────────
create table if not exists public.notifications (
    id           uuid primary key default gen_random_uuid(),
    lead_id      uuid not null references public.leads(id) on delete cascade,
    tipo         text not null,                       -- qualified_lead / human_request
    payload      jsonb not null,
    mensagem     text not null,                       -- texto final enviado ao grupo
    enviado_em   timestamptz not null default now(),
    sucesso      boolean not null default true,
    erro         text
);

create index if not exists idx_notifications_lead on public.notifications(lead_id);
create index if not exists idx_notifications_tipo on public.notifications(tipo);

-- ── trigger pra updated_at em leads ──────────────────────────
create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_leads_updated_at on public.leads;
create trigger trg_leads_updated_at
    before update on public.leads
    for each row execute function public.set_updated_at();
