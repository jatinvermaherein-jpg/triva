begin;

create table public.ks_config (
  guild_id text primary key,
  staff_role_id text not null,
  blocked_role_id text not null,
  channels jsonb not null,
  currency_name text not null default 'Server Coins',
  created_at timestamptz not null default now()
);

create table public.ks_seasons (
  id uuid primary key default gen_random_uuid(),
  guild_id text not null references public.ks_config(guild_id),
  number integer not null check (number > 0),
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  state text not null default 'open'
    check (state in ('open', 'finalizing', 'finalized')),
  finalized_at timestamptz,
  unique (guild_id, number),
  check (ends_at = starts_at + interval '28 days')
);

create table public.ks_slots (
  id uuid primary key default gen_random_uuid(),
  season_id uuid not null references public.ks_seasons(id),
  league text not null check (league in ('knowledge', 'strategy', 'hangar')),
  opens_at timestamptz not null,
  closes_at timestamptz not null,
  state text not null default 'draft'
    check (state in ('draft', 'ready', 'posted', 'missed', 'cancelled')),
  late_approved boolean not null default false,
  unique (season_id, league, opens_at),
  check (closes_at = opens_at + interval '24 hours')
);

create table public.ks_challenges (
  id uuid primary key default gen_random_uuid(),
  slot_id uuid not null references public.ks_slots(id),
  title text not null,
  prompt text not null,
  points integer not null default 10 check (points between 1 and 10),
  options jsonb,
  correct_index integer,
  author_id text not null,
  state text not null default 'scheduled'
    check (state in ('scheduled', 'open', 'closed', 'cancelled')),
  message_id text,
  created_at timestamptz not null default now(),
  check (
    (options is null and correct_index is null)
    or
    (
      jsonb_typeof(options) = 'array'
      and jsonb_array_length(options) between 2 and 5
      and correct_index >= 0
      and correct_index < jsonb_array_length(options)
    )
  )
);

create table public.ks_answers (
  id uuid primary key default gen_random_uuid(),
  challenge_id uuid not null references public.ks_challenges(id),
  user_id text not null,
  draft jsonb not null default '[]'::jsonb,
  body jsonb,
  choice integer,
  submitted_at timestamptz,
  updated_at timestamptz not null default now(),
  version integer not null default 0,
  score integer check (score between 0 and 10),
  scored_by text,
  scored_at timestamptz,
  disqualified boolean not null default false,
  disqualification_reason text,
  review_message_id text,
  published_version integer,
  unique (challenge_id, user_id)
);

create table public.ks_answer_versions (
  id bigint generated always as identity primary key,
  answer_id uuid not null references public.ks_answers(id),
  version integer not null,
  body jsonb not null,
  actor_id text not null,
  created_at timestamptz not null default now(),
  unique (answer_id, version)
);

create table public.ks_uploads (
  id uuid primary key default gen_random_uuid(),
  answer_id uuid not null references public.ks_answers(id),
  user_id text not null,
  path text not null unique,
  filename text not null,
  mime text not null,
  bytes integer not null check (bytes > 0 and bytes <= 10485760),
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table public.ks_workspaces (
  thread_id text primary key,
  challenge_id uuid not null references public.ks_challenges(id),
  user_id text not null,
  unique (challenge_id, user_id)
);

create table public.ks_exclusions (
  guild_id text not null references public.ks_config(guild_id),
  user_id text not null,
  reason text not null,
  actor_id text not null,
  created_at timestamptz not null default now(),
  primary key (guild_id, user_id)
);

create table public.ks_tiebreaks (
  season_id uuid not null references public.ks_seasons(id),
  league text not null check (league in ('knowledge', 'strategy', 'hangar')),
  user_id text not null,
  value integer not null default 0,
  reason text not null,
  actor_id text not null,
  primary key (season_id, league, user_id)
);

create table public.ks_events (
  id bigint generated always as identity primary key,
  guild_id text not null,
  actor_id text not null,
  kind text not null,
  data jsonb not null default '{}'::jsonb,
  delivered boolean not null default false,
  created_at timestamptz not null default now()
);

create table public.ks_artifacts (
  key text primary key,
  channel_id text not null,
  message_id text,
  created_at timestamptz not null default now()
);

create table public.ks_awards (
  id uuid primary key default gen_random_uuid(),
  season_id uuid not null references public.ks_seasons(id),
  league text not null check (league in ('knowledge', 'strategy', 'hangar')),
  user_id text not null,
  display_name text not null,
  placement integer not null check (placement between 1 and 10),
  points integer not null,
  coins integer not null,
  reward_status text not null default 'pending'
    check (reward_status in ('pending', 'approved', 'paid')),
  reward_actor_id text,
  reward_updated_at timestamptz,
  role_id text,
  role_done boolean not null default false,
  unique (season_id, league, placement)
);

create index ks_answers_user_idx on public.ks_answers(user_id);
create index ks_slots_open_idx on public.ks_slots(opens_at);
create index ks_events_delivery_idx on public.ks_events(delivered, id);
create index ks_uploads_answer_idx on public.ks_uploads(answer_id);

-- Historical score decisions and challenge edits go in the event log.
-- Submission bodies are versioned separately.

create or replace view public.ks_context as
select
  c.*,
  sl.season_id,
  sl.league,
  sl.opens_at,
  sl.closes_at,
  sl.state as slot_state,
  se.guild_id,
  se.number as season_number,
  se.state as season_state,
  se.starts_at as season_starts_at,
  se.ends_at as season_ends_at
from public.ks_challenges c
join public.ks_slots sl on sl.id = c.slot_id
join public.ks_seasons se on se.id = sl.season_id;

-- Trivia results remain absent from public standings until closing.
-- First-correct bonus is derived, not permanently attached to a user.
-- Disqualifying the first correct answer moves the bonus automatically.

create or replace view public.ks_answer_points as
with eligible as (
  select
    a.*,
    c.season_id,
    c.guild_id,
    c.league,
    c.closes_at,
    c.points as question_points
  from public.ks_answers a
  join public.ks_context c on c.id = a.challenge_id
  where
    a.submitted_at is not null
    and not a.disqualified
    and c.state <> 'cancelled'
    and c.slot_state <> 'cancelled'
    and (
      c.league <> 'knowledge'
      or c.closes_at <= now()
    )
),
first_correct as (
  select distinct on (challenge_id)
    challenge_id,
    id as answer_id
  from eligible
  where league = 'knowledge' and score > 0
  order by challenge_id, submitted_at, id
)
select
  e.*,
  coalesce(e.score, 0)
    + case when f.answer_id = e.id then 1 else 0 end as earned
from eligible e
left join first_correct f on f.challenge_id = e.challenge_id;

create or replace view public.ks_standings as
select
  p.guild_id,
  p.season_id,
  p.league,
  p.user_id,
  sum(p.earned)::integer as points,
  count(*) filter (
    where p.league = 'knowledge' and p.score > 0
  )::integer as correct_count,
  array[
    count(*) filter (where p.score = 10),
    count(*) filter (where p.score = 9),
    count(*) filter (where p.score = 8),
    count(*) filter (where p.score = 7),
    count(*) filter (where p.score = 6),
    count(*) filter (where p.score = 5),
    count(*) filter (where p.score = 4),
    count(*) filter (where p.score = 3),
    count(*) filter (where p.score = 2),
    count(*) filter (where p.score = 1),
    count(*) filter (where p.score = 0)
  ]::integer[] as histogram,
  coalesce(t.value, 0) as tiebreak
from public.ks_answer_points p
left join public.ks_tiebreaks t
  on t.season_id = p.season_id
  and t.league = p.league
  and t.user_id = p.user_id
group by
  p.guild_id, p.season_id, p.league, p.user_id, t.value;

create or replace function public.ks_word_count(p_sections jsonb)
returns integer
language sql
immutable
set search_path = public, pg_temp
as $$
  select case
    when btrim(coalesce(string_agg(value, ' '), '')) = '' then 0
    else cardinality(
      regexp_split_to_array(btrim(string_agg(value, ' ')), E'\\s+')
    )
  end
  from jsonb_array_elements_text(p_sections)
$$;

-- Mutations that race against deadlines or grading use a DB transaction.
create or replace function public.ks_answer_action(
  p_actor text,
  p_challenge uuid,
  p_kind text,
  p_payload jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  c record;
  a public.ks_answers%rowtype;
  v_now timestamptz;
  v_sections jsonb;
  v_body jsonb;
  v_choice integer;
  v_total_chars integer;
begin
  select * into c from public.ks_context where id = p_challenge;

  if not found then
    raise exception 'Challenge not found';
  end if;

  -- Serializes answer mutations with season finalization.
  perform 1 from public.ks_seasons
    where id = c.season_id for update;

  select state into c.season_state
    from public.ks_seasons where id = c.season_id;

  v_now := clock_timestamp();

  if c.season_state <> 'open' then
    raise exception 'This season is locked';
  end if;

  if c.state <> 'open'
    or c.slot_state = 'cancelled'
    or v_now < c.opens_at
    or v_now >= c.closes_at
  then
    raise exception 'This challenge is not accepting answers';
  end if;

  if exists (
    select 1 from public.ks_exclusions
    where guild_id = c.guild_id and user_id = p_actor
  ) then
    raise exception 'Participation is restricted';
  end if;

  if c.author_id = p_actor then
    raise exception 'Challenge authors cannot enter their own challenge';
  end if;

  if p_kind = 'trivia' then
    if c.league <> 'knowledge' then
      raise exception 'Not a Trivia question';
    end if;

    v_choice := (p_payload->>'choice')::integer;

    if v_choice is null
      or v_choice < 0
      or v_choice >= jsonb_array_length(c.options)
    then
      raise exception 'Invalid answer option';
    end if;

    insert into public.ks_answers (
      challenge_id, user_id, choice, submitted_at,
      score, scored_by, scored_at, version
    ) values (
      c.id, p_actor, v_choice, v_now,
      case when v_choice = c.correct_index then c.points else 0 end,
      'BOT', v_now, 1
    );

    -- Deliberately do not return correctness.
    return jsonb_build_object('accepted', true);
  end if;

  if c.league = 'knowledge' then
    raise exception 'Trivia answers cannot be edited';
  end if;

  insert into public.ks_answers(challenge_id, user_id)
  values(c.id, p_actor)
  on conflict(challenge_id, user_id) do nothing;

  select * into a from public.ks_answers
  where challenge_id = c.id and user_id = p_actor
  for update;

  if p_kind = 'save' then
    v_sections := p_payload->'sections';

    if v_sections is null or jsonb_typeof(v_sections) <> 'array' then
      raise exception 'Invalid draft';
    end if;

    if jsonb_array_length(v_sections) > 10 then
      raise exception 'Maximum 10 sections';
    end if;

    if exists (
      select 1 from jsonb_array_elements(v_sections) x
      where jsonb_typeof(x) <> 'string'
    ) then
      raise exception 'Sections must contain text';
    end if;

    select coalesce(sum(length(value)), 0)::integer
    into v_total_chars
    from jsonb_array_elements_text(v_sections);

    if public.ks_word_count(v_sections) > 2000
      or v_total_chars > 20000
    then
      raise exception 'Answer exceeds 2,000 words or 20,000 characters';
    end if;

    if exists (
      select 1 from jsonb_array_elements_text(v_sections)
      where length(value) > 3900
    ) then
      raise exception 'Each section may contain at most 3,900 characters';
    end if;

    update public.ks_answers
    set draft = v_sections, updated_at = v_now
    where id = a.id;

  elsif p_kind = 'submit' then
    if public.ks_word_count(a.draft) = 0 then
      raise exception 'Add answer text before submitting';
    end if;

    select jsonb_build_object(
      'sections', a.draft,
      'uploads', coalesce(
        jsonb_agg(id) filter (where id is not null), '[]'::jsonb
      )
    )
    into v_body
    from public.ks_uploads
    where answer_id = a.id and active;

    update public.ks_answers
    set
      body = v_body,
      submitted_at = coalesce(submitted_at, v_now),
      updated_at = v_now,
      version = version + 1
    where id = a.id
    returning * into a;

    insert into public.ks_answer_versions(
      answer_id, version, body, actor_id
    ) values(a.id, a.version, a.body, p_actor);

    insert into public.ks_events(guild_id, actor_id, kind, data)
    values(
      c.guild_id, p_actor, 'answer.submitted',
      jsonb_build_object(
        'answer_id', a.id,
        'challenge_id', c.id,
        'version', a.version
      )
    );

  else
    raise exception 'Unknown answer operation';
  end if;

  return jsonb_build_object('answer_id', a.id, 'accepted', true);
end;
$$;

create or replace function public.ks_score_answer(
  p_actor text,
  p_answer uuid,
  p_expected_version integer,
  p_score integer,
  p_disqualified boolean,
  p_reason text
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  a public.ks_answers%rowtype;
  c record;
  v_state text;
begin
  select * into a from public.ks_answers where id = p_answer;

  if not found then
    raise exception 'Answer not found';
  end if;

  select * into c from public.ks_context where id = a.challenge_id;

  select state into v_state from public.ks_seasons
    where id = c.season_id for update;

  select * into a from public.ks_answers where id = p_answer for update;

  if v_state <> 'open' then
    raise exception 'Season is locked';
  end if;

  if a.user_id = p_actor then
    raise exception 'You cannot score your own answer';
  end if;

  if a.submitted_at is null then
    raise exception 'This is an unsubmitted draft';
  end if;

  if clock_timestamp() < c.closes_at then
    raise exception 'Scoring opens after the submission deadline';
  end if;

  if c.state = 'cancelled' or c.slot_state = 'cancelled' then
    raise exception 'Challenge was cancelled';
  end if;

  if a.version <> p_expected_version then
    raise exception 'Submission changed. Refresh the review card';
  end if;

  if p_score is null or p_score < 0 or p_score > 10 then
    raise exception 'Score must be a whole number from 0 to 10';
  end if;

  if c.league = 'knowledge'
    and p_score <> coalesce(a.score, 0)
  then
    raise exception 'Trivia base scores are derived from the answer key';
  end if;

  if length(btrim(coalesce(p_reason, ''))) = 0 then
    raise exception 'A reason or feedback is required';
  end if;

  update public.ks_answers
  set
    score = p_score,
    disqualified = p_disqualified,
    disqualification_reason =
      case when p_disqualified then p_reason else null end,
    scored_by = p_actor,
    scored_at = clock_timestamp()
  where id = a.id;

  insert into public.ks_events(guild_id, actor_id, kind, data)
  values(
    c.guild_id, p_actor, 'answer.scored',
    jsonb_build_object(
      'answer_id', a.id,
      'user_id', a.user_id,
      'old_score', a.score,
      'new_score', p_score,
      'old_disqualified', a.disqualified,
      'disqualified', p_disqualified,
      'reason', p_reason,
      'version', a.version
    )
  );
end;
$$;

-- Staff decide tie-break outcomes after a real tie-break activity.
create or replace function public.ks_set_tiebreak(
  p_actor text,
  p_season uuid,
  p_league text,
  p_user text,
  p_value integer,
  p_reason text
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  s public.ks_seasons%rowtype;
begin
  select * into s from public.ks_seasons
    where id = p_season for update;

  if not found or s.state <> 'open' then
    raise exception 'Season is locked or missing';
  end if;

  if length(btrim(coalesce(p_reason, ''))) = 0 then
    raise exception 'Document the tie-break result';
  end if;

  insert into public.ks_tiebreaks(
    season_id, league, user_id, value, reason, actor_id
  ) values(
    p_season, p_league, p_user, p_value, p_reason, p_actor
  )
  on conflict(season_id, league, user_id) do update
  set value = excluded.value,
      reason = excluded.reason,
      actor_id = excluded.actor_id;

  insert into public.ks_events(guild_id, actor_id, kind, data)
  values(
    s.guild_id, p_actor, 'tiebreak.recorded',
    jsonb_build_object(
      'season_id', p_season, 'league', p_league,
      'user_id', p_user, 'value', p_value, 'reason', p_reason
    )
  );
end;
$$;

-- Names/absent users are supplied only by the trusted bot backend.
-- Awards are frozen snapshots, not a live view.
create or replace function public.ks_finalize(
  p_actor text,
  p_season uuid,
  p_absent text[],
  p_names jsonb
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  s public.ks_seasons%rowtype;
  v_pending integer;
  v_tied boolean;
begin
  select * into s from public.ks_seasons
    where id = p_season for update;

  if not found or s.state <> 'open' then
    raise exception 'Season already locked or missing';
  end if;

  if clock_timestamp() < s.ends_at then
    raise exception 'The four-week season has not ended';
  end if;

  select count(*) into v_pending
  from public.ks_answers a
  join public.ks_context c on c.id = a.challenge_id
  where c.season_id = s.id
    and c.league <> 'knowledge'
    and c.state <> 'cancelled'
    and c.slot_state <> 'cancelled'
    and a.submitted_at is not null
    and a.score is null
    and not a.disqualified;

  if v_pending > 0 then
    raise exception '% submissions still need scoring', v_pending;
  end if;

  with ranked as (
    select
      st.*,
      case
        when league = 'knowledge' then array[correct_count]
        else histogram
      end as secondary,
      row_number() over (
        partition by league
        order by
          points desc,
          case
            when league = 'knowledge' then array[correct_count]
            else histogram
          end desc,
          tiebreak desc,
          user_id
      ) as position
    from public.ks_standings st
    where season_id = s.id
      and points > 0
      and not (user_id = any(coalesce(p_absent, array[]::text[])))
  )
  select exists (
    select 1 from ranked
    group by league, points, secondary, tiebreak
    having count(*) > 1 and min(position) <= 10
  ) into v_tied;

  if v_tied then
    raise exception 'An unresolved tie affects the top 10';
  end if;

  insert into public.ks_awards(
    season_id, league, user_id, display_name,
    placement, points, coins
  )
  select
    s.id,
    league,
    user_id,
    coalesce(p_names->>user_id, user_id),
    position::integer,
    points,
    (array[3000,2000,1000,800,700,600,500,400,300,200])
      [position::integer]
  from (
    select
      st.*,
      row_number() over (
        partition by league
        order by
          points desc,
          case
            when league = 'knowledge' then array[correct_count]
            else histogram
          end desc,
          tiebreak desc,
          user_id
      ) as position
    from public.ks_standings st
    where season_id = s.id
      and points > 0
      and not (user_id = any(coalesce(p_absent, array[]::text[])))
  ) x
  where position <= 10;

  update public.ks_seasons
  set state = 'finalizing'
  where id = s.id;

  insert into public.ks_events(guild_id, actor_id, kind, data)
  values(
    s.guild_id, p_actor, 'season.approved',
    jsonb_build_object('season_id', s.id, 'absent_users', p_absent)
  );
end;
$$;

-- Deny all direct public/client access.
do $$
declare r record;
begin
  for r in
    select tablename from pg_tables
    where schemaname = 'public' and tablename like 'ks_%'
  loop
    execute format(
      'alter table public.%I enable row level security', r.tablename
    );
    execute format(
      'revoke all on public.%I from anon, authenticated', r.tablename
    );
    execute format(
      'grant all on public.%I to service_role', r.tablename
    );
  end loop;
end;
$$;

revoke all on public.ks_context,
  public.ks_answer_points,
  public.ks_standings
from anon, authenticated;

grant select on public.ks_context,
  public.ks_answer_points,
  public.ks_standings
to service_role;

revoke all on function public.ks_word_count(jsonb)
  from public, anon, authenticated;

revoke all on function public.ks_answer_action(text, uuid, text, jsonb)
  from public, anon, authenticated;

revoke all on function public.ks_score_answer(
  text, uuid, integer, integer, boolean, text
) from public, anon, authenticated;

revoke all on function public.ks_set_tiebreak(
  text, uuid, text, text, integer, text
) from public, anon, authenticated;

revoke all on function public.ks_finalize(text, uuid, text[], jsonb)
  from public, anon, authenticated;

grant execute on function public.ks_word_count(jsonb) to service_role;

grant execute on function public.ks_answer_action(
  text, uuid, text, jsonb
) to service_role;

grant execute on function public.ks_score_answer(
  text, uuid, integer, integer, boolean, text
) to service_role;

grant execute on function public.ks_set_tiebreak(
  text, uuid, text, text, integer, text
) to service_role;

grant execute on function public.ks_finalize(
  text, uuid, text[], jsonb
) to service_role;

grant usage, select on all sequences in schema public to service_role;

insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types)
values(
  'knowledge-uploads',
  'knowledge-uploads',
  false,
  10485760,
  array['image/png', 'image/jpeg', 'image/webp', 'image/gif']
)
on conflict(id) do nothing;

commit;
