-- 模型配置（Phase 1 最小版本）：每个工作区按 kind（chat / embedding）登记可选模型。
-- 只存「凭据引用」（例如环境变量名），不落明文密钥，避免密钥进入数据库与日志。
CREATE TABLE IF NOT EXISTS app.model_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES app.workspaces(id) ON DELETE CASCADE,
    name varchar(128) NOT NULL,
    kind varchar(16) NOT NULL,
    provider varchar(64) NOT NULL,
    model_name varchar(128) NOT NULL,
    base_url text,
    credential_ref varchar(128),
    is_default boolean NOT NULL DEFAULT false,
    created_by uuid REFERENCES app.users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT model_configs_kind_check CHECK (kind IN ('chat', 'embedding'))
);

CREATE UNIQUE INDEX IF NOT EXISTS model_configs_workspace_kind_name_idx
    ON app.model_configs (workspace_id, kind, name);

-- 同一工作区同一 kind 只允许一个默认模型
CREATE UNIQUE INDEX IF NOT EXISTS model_configs_workspace_kind_default_idx
    ON app.model_configs (workspace_id, kind)
    WHERE is_default;
