# backend/migrations/rag_v2_migration.py
"""
RAG v2 迁移脚本：
1) rag_documents 增加 chunker_version / embedding_model / parent_count
2) 新建 ai_query_logs（查询日志，任务 1.4/1.5 使用）
3) 新建 ai_feedback（用户反馈）
幂等：可重复执行。
"""
from sqlalchemy import text
from backend.database import engine


def _column_exists(conn, table, column):
    row = conn.execute(text("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = :t AND COLUMN_NAME = :c
    """), {"t": table, "c": column}).scalar()
    return row and row > 0

print("[migrate] engine.url =", engine.url)
print("[migrate] engine.url.database =", engine.url.database)
def migrate():
    with engine.connect() as conn:
        # ---- rag_documents 扩列 ----
        additions = [
            ("chunker_version", "VARCHAR(20) DEFAULT 'v1'"),
            ("embedding_model", "VARCHAR(100) DEFAULT ''"),
            ("parent_count", "INT DEFAULT 0"),
        ]
        for col, ddl in additions:
            if not _column_exists(conn, "rag_documents", col):
                conn.execute(text(f"ALTER TABLE rag_documents ADD COLUMN {col} {ddl}"))
                print(f"[migrate] rag_documents + {col}")

        # ---- ai_query_logs ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_query_logs (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                trace_id CHAR(32),
                raw_query TEXT,
                rewritten_query TEXT,
                intent VARCHAR(50),
                sentiment VARCHAR(20),
                emotion VARCHAR(30),
                urgency VARCHAR(20),
                need_rag TINYINT DEFAULT 0,
                filters_json JSON,
                retrieved_json JSON,
                rerank_json JSON,
                latency_ms INT,
                model VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_created (user_id, created_at),
                INDEX idx_intent_created (intent, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        print("[migrate] ai_query_logs ready")

        # ---- ai_feedback ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                log_id BIGINT,
                user_id INT,
                rating TINYINT,
                reason VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_log (log_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        print("[migrate] ai_feedback ready")

        conn.commit()
        print("[migrate] done")


if __name__ == "__main__":
    migrate()