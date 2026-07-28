-- Follow-up automático de leads inativos

create table if not exists public.follow_ups (
    id             uuid primary key default gen_random_uuid(),
    lead_id        uuid not null references public.leads(id) on delete cascade,
    tentativa      int  not null check (tentativa between 1 and 3),
    mensagem       text not null,
    enviado_em     timestamptz not null default now(),
    sucesso        boolean not null default true,
    erro           text,
    message_id_wpp text
);

create index if not exists idx_follow_ups_lead on public.follow_ups(lead_id, enviado_em);
create index if not exists idx_follow_ups_enviado_em on public.follow_ups(enviado_em);
