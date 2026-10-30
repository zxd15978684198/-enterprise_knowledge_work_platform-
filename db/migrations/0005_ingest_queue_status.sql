-- W22：入库分派引入 queued 状态（API 落库后立即返回，worker 异步推进）。
-- 只扩状态集合，不改既有语义：pending/processing/ready/failed 保持不变。
ALTER TABLE app.documents
    DROP CONSTRAINT documents_status_check;

ALTER TABLE app.documents
    ADD CONSTRAINT documents_status_check
        CHECK (status IN ('pending', 'queued', 'processing', 'ready', 'failed'));

COMMENT ON COLUMN app.documents.status IS
    'pending=已落库待处理；queued=已投递 Celery；processing=worker 执行中；ready=可检索；failed=失败';
