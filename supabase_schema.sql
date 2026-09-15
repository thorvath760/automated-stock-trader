-- Run this entire file in Supabase: SQL Editor > New query > Run.
create table if not exists public.client_profiles (
  user_id text primary key,
  email text not null,
  display_name text,
  strategy_config jsonb not null default '{}'::jsonb,
  risk_result jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.disclosure_acceptances (
  id bigint generated always as identity primary key,
  user_id text not null references public.client_profiles(user_id) on delete cascade,
  disclosure_version text not null,
  accepted_at timestamptz not null default now(),
  unique (user_id, disclosure_version)
);

create table if not exists public.broker_connections (
  id bigint generated always as identity primary key,
  user_id text not null references public.client_profiles(user_id) on delete cascade,
  provider text not null default 'alpaca',
  environment text not null default 'paper' check (environment = 'paper'),
  status text not null default 'not_connected',
  provider_account_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, provider)
);

alter table public.client_profiles enable row level security;
alter table public.disclosure_acceptances enable row level security;
alter table public.broker_connections enable row level security;

-- This Streamlit build performs database work only on its trusted Python server
-- using the service-role key. Never put that key in app.py, GitHub, or browser code.
-- A production launch should move these operations to a dedicated API and add
-- end-user JWT policies before enabling brokerage connections.

