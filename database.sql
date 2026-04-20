-- 할일 항목 상태 enum
CREATE TYPE todo_status AS ENUM ('NEW', 'TODO', 'IN_PROGRESS', 'SUBMITTED', 'PENDING', 'DONE', 'CANCELLED');
-- 에이전트 모드 enum
CREATE TYPE agent_mode AS ENUM ('DRAFT', 'COMPLETE');
-- 드래프트 상태 enum
CREATE TYPE draft_status AS ENUM ('STARTED', 'CANCELLED', 'COMPLETED', 'FB_REQUESTED', 'HUMAN_ASKED', 'FAILED');
-- 이벤트 타입 enum
CREATE TYPE event_type_enum AS ENUM (
  'task_started',
  'task_completed',
  'tool_usage_started',
  'tool_usage_finished',
  'crew_completed',
  'human_asked',
  'human_response',
  'human_checked',
  'task_working',
  'error'
);
-- 이벤트 상태 enum
CREATE TYPE event_status AS ENUM ('ASKED', 'APPROVED', 'REJECTED');


create table
  public.todolist (
    id uuid not null,
    user_id text null,
    username text null,
    proc_inst_id text null,
    root_proc_inst_id text null,
    execution_scope text null,
    proc_def_id text null,
    activity_id text null,
    activity_name text null,
    start_date timestamp without time zone null,
    end_date timestamp without time zone null,
    description text null,
    tool text null,
    due_date timestamp without time zone null,
    tenant_id text null default tenant_id (),
    reference_ids text[] null,
    adhoc boolean null default false,
    assignees jsonb null,
    duration integer null,
    output jsonb null,
    retry integer null default 0,
    consumer text null,
    log text null,
    project_id uuid null,
    draft jsonb null,
    feedback jsonb null,
    updated_at timestamp with time zone null default now(),
    temp_feedback text null,
    output_url text null,
    rework_count integer null default 0,
    status public.todo_status null,
    agent_mode public.agent_mode null,
    query text null,
    agent_orch text null,
    version_tag text null,
    version text null,
    feedback_status text null,
    draft_status public.draft_status null,
    constraint todolist_pkey primary key (id),
    constraint todolist_tenant_id_fkey foreign key (tenant_id) references tenants (id) on update cascade on delete cascade
  ) tablespace pg_default;

create index if not exists idx_todolist_procinst on public.todolist using btree (proc_inst_id) tablespace pg_default;

create index if not exists idx_todolist_inprog on public.todolist using btree (agent_orch, tenant_id, start_date)
where
  (status = 'IN_PROGRESS'::todo_status) tablespace pg_default;

create trigger trigger_update_bpm_proc_inst_updated_at
after
update on todolist for each row
execute function update_bpm_proc_inst_updated_at ();

create trigger set_updated_at before
update on todolist for each row
execute function update_updated_at_column ();

create trigger todolist_change_trigger
after insert
or
update on todolist for each row
execute function handle_todolist_change ();

create trigger update_user_id_trigger
after
update on todolist for each row when (old.user_id is distinct from new.user_id)
execute function update_notification_user_id ();

create trigger delete_notification_trigger
after delete on todolist for each row
execute function delete_notification_on_todolist_delete ();



create table if not exists public.form_def (
    uuid uuid not null default gen_random_uuid (),
    html text not null,
    proc_def_id text not null,
    activity_id text not null,
    tenant_id text null default public.tenant_id(),
    id text null default ''::text,
    fields_json jsonb null,
    constraint form_def_pkey primary key (uuid),
    constraint form_def_tenant_id_fkey foreign key (tenant_id) references tenants (id) on update cascade on delete cascade
) tablespace pg_default;



create table
  public.users (
    id uuid not null,
    username text null,
    profile text null default '/images/defaultUser.png'::text,
    email text null,
    is_admin boolean not null default false,
    role text null,
    tenant_id text not null,
    device_token text null,
    goal text null,
    persona text null,
    endpoint text null,
    description text null,
    tools text null,
    skills text null,
    is_agent boolean not null default false,
    model text null,
    agent_type text null,
    alias text null,
    tool_priority jsonb null,
    constraint users_pkey primary key (id, tenant_id),
    constraint users_tenant_id_fkey foreign key (tenant_id) references tenants (id) on update cascade on delete cascade
  ) tablespace pg_default;

create index if not exists idx_users_is_agent_true on public.users using btree (is_agent)
where
  (is_agent = true) tablespace pg_default;



create table
  public.events (
    id text not null,
    job_id text not null,
    todo_id text null,
    proc_inst_id text null,
    event_type public.event_type_enum not null,
    status public.event_status null,
    crew_type text null,
    data jsonb not null,
    timestamp timestamp with time zone null default now(),
    constraint events_pkey primary key (id)
  ) tablespace pg_default;

DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text);

-- 0) 공용 대기 작업 조회 및 상태 변경 (agent_orch 인자로 필터)
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text);

CREATE OR REPLACE FUNCTION public.fetch_pending_task(
  p_agent_orch text,
  p_consumer   text,
  p_limit      integer,
  p_env        text
)
RETURNS SETOF todolist
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT t.id
      FROM todolist AS t
      WHERE t.status = 'IN_PROGRESS'
        -- env 분기 제거됨
        -- agent_orch 필터(옵션)
        AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch)
        -- 처리 대상 선택 로직
        AND (
          (t.agent_mode IN ('DRAFT','COMPLETE') AND t.draft IS NULL AND t.draft_status IS NULL)
          OR t.draft_status = 'FB_REQUESTED'
        )
      ORDER BY t.start_date
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED
    ),
    upd AS (
      UPDATE todolist AS t
         SET draft_status = 'STARTED',
             consumer     = p_consumer
        FROM cte
       WHERE t.id = cte.id
       RETURNING t.*
    )
    SELECT * FROM upd;
END;
$$;

GRANT EXECUTE ON FUNCTION public.fetch_pending_task(text, text, integer, text) TO anon;


create table public.tenants (
  id text not null,
  owner uuid null default auth.uid (),
  is_deleted boolean not null default false,
  deleted_at timestamp with time zone null,
  mcp jsonb null,
  skills text[] null,
  constraint tenants_pkey primary key (id)
) TABLESPACE pg_default;


create table if not exists public.chat_rooms (
    id text not null,
    participants jsonb not null,
    message jsonb null,
    name text null,
    primary_agent_id text null,
    tenant_id text null default public.tenant_id(),
    context jsonb null,
    constraint chat_rooms_pkey primary key (id),
    constraint chat_rooms_tenant_id_fkey foreign key (tenant_id) references tenants (id) on update cascade on delete cascade
) tablespace pg_default;

create table if not exists public.chats (
    uuid text not null,
    id text not null,
    messages jsonb null,
    tenant_id text null default public.tenant_id(),
    thread_id text null,
    constraint chats_pkey primary key (uuid),
    constraint chats_tenant_id_fkey foreign key (tenant_id) references tenants (id) on update cascade on delete cascade
) tablespace pg_default;

create table if not exists public.chat_attachments (
    id text not null,
    file_name text null,
    file_path text null,
    chat_room_id text null,
    user_name text null,
    created_at timestamp with time zone not null default now(),
    tenant_id text null,
    constraint chat_attachments_pkey primary key (id),
    constraint chat_attachments_chat_room_id_fkey foreign KEY (chat_room_id) references chat_rooms (id) on update CASCADE on delete CASCADE,
    constraint chat_attachments_tenant_id_fkey foreign KEY (tenant_id) references tenants (id)
) TABLESPACE pg_default;


