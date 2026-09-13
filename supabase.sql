-- FinPilot v0.5 persistence schema.
-- This migration is already applied to the connected Supabase project when this package was prepared.
create table if not exists public.fp_profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  demo_user_id text not null default 'user_26',
  created_at timestamptz not null default now()
);

-- Automatically create a private FinPilot profile for every new Auth account.
-- The demo_user_id is only a temporary mapping to the supplied demo dataset;
-- the production financial-data tables will replace this mapping in a later phase.
create or replace function public.fp_handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.fp_profiles (id, display_name, demo_user_id)
  values (new.id, coalesce(new.raw_user_meta_data->>'full_name', split_part(coalesce(new.email, 'FinPilot user'), '@', 1)), 'user_26')
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists fp_on_auth_user_created on auth.users;
create trigger fp_on_auth_user_created
  after insert on auth.users
  for each row execute function public.fp_handle_new_user();
create table if not exists public.fp_conversations (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default 'New chat', created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.fp_messages (
  id uuid primary key default gen_random_uuid(), conversation_id uuid not null references public.fp_conversations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade, role text not null check (role in ('user','assistant')),
  content text not null, created_at timestamptz not null default now()
);
alter table public.fp_profiles enable row level security;
alter table public.fp_conversations enable row level security;
alter table public.fp_messages enable row level security;
create policy "fp_profiles_select_own" on public.fp_profiles for select using (auth.uid() = id);
create policy "fp_profiles_insert_own" on public.fp_profiles for insert with check (auth.uid() = id);
create policy "fp_profiles_update_own" on public.fp_profiles for update using (auth.uid() = id) with check (auth.uid() = id);
create policy "fp_conversations_select_own" on public.fp_conversations for select using (auth.uid() = user_id);
create policy "fp_conversations_insert_own" on public.fp_conversations for insert with check (auth.uid() = user_id);
create policy "fp_conversations_update_own" on public.fp_conversations for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "fp_conversations_delete_own" on public.fp_conversations for delete using (auth.uid() = user_id);
create policy "fp_messages_select_own" on public.fp_messages for select using (auth.uid() = user_id);
create policy "fp_messages_insert_own" on public.fp_messages for insert with check (auth.uid() = user_id);
create policy "fp_messages_delete_own" on public.fp_messages for delete using (auth.uid() = user_id);
