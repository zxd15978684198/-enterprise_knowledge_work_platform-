-- 0001_identity.sql —— 身份与工作区（Phase 1 / W18）
-- 依赖：pgcrypto（gen_random_uuid）与 citext 可选；本文件只用内置函数。

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE app.users (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text        NOT NULL,
    password_hash text        NOT NULL,
    display_name  text        NOT NULL DEFAULT '',
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_email_not_blank CHECK (btrim(email) <> ''),
    CONSTRAINT users_email_len CHECK (char_length(email) <= 320)
);

CREATE UNIQUE INDEX users_email_lower_key ON app.users (lower(email));

CREATE TABLE app.workspaces (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL,
    slug       text        NOT NULL UNIQUE,
    owner_id   uuid        NOT NULL REFERENCES app.users (id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT workspaces_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT workspaces_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$')
);

CREATE INDEX workspaces_owner_idx ON app.workspaces (owner_id);
CREATE INDEX workspaces_active_idx ON app.workspaces (id) WHERE deleted_at IS NULL;

CREATE TABLE app.memberships (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    user_id      uuid        NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
    role         text        NOT NULL DEFAULT 'member',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memberships_workspace_user_key UNIQUE (workspace_id, user_id),
    CONSTRAINT memberships_role_check CHECK (role IN ('owner', 'member'))
);

CREATE INDEX memberships_user_idx ON app.memberships (user_id);
CREATE INDEX memberships_workspace_idx ON app.memberships (workspace_id);

-- 一个工作区有且只有一个 owner 成员行：允许转移但禁止并存。
CREATE UNIQUE INDEX memberships_single_owner_key
    ON app.memberships (workspace_id)
    WHERE role = 'owner';

-- updated_at 自动维护
CREATE OR REPLACE FUNCTION app.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER users_touch_updated_at
    BEFORE UPDATE ON app.users
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

CREATE TRIGGER workspaces_touch_updated_at
    BEFORE UPDATE ON app.workspaces
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

CREATE TRIGGER memberships_touch_updated_at
    BEFORE UPDATE ON app.memberships
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();
