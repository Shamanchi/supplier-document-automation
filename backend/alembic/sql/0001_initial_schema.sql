-- =====================================================================
-- Фаза 3 «DocPrice» — референсный DDL PostgreSQL
-- ---------------------------------------------------------------------
-- Источник истины: docs/architecture/PHASE3_CANONICAL_ARCHITECTURE.md §10,
--                  docs/architecture/DATA_MODEL.md, docs/architecture/DB_SCHEMA.md
-- Назначение: черновая, но полная схема как основа миграций инкремента M0.
-- СУБД: PostgreSQL 15+.
-- ---------------------------------------------------------------------
-- ГРАНИЦЫ (решение: гибридный вариант, DB_SCHEMA_CHANGELIST.md):
--   Этот файл описывает ТОЛЬКО операционный контур Фазы 3.
--   Товарная витрина Фазы 2 — в db/schema_phase2_showcase.sql и сюда не входит:
--   products, product_attributes, product_documents, price_history, attributes,
--   categories, suppliers, customers, canonical_names, canonical_templates,
--   tenders, tender_requirements.
--   Связь контуров — только через showcase_product_refs (§16); FK через границу запрещён.
-- Соглашения:
--   * PK — UUID (gen_random_uuid, расширение pgcrypto);
--   * версии/optimistic lock — колонка version INTEGER;
--   * денежные/сравниваемые величины — NUMERIC (не float);
--   * created_at/updated_at — TIMESTAMPTZ; updated_at ведёт приложение/триггер;
--   * неизменяемые таблицы защищены триггером trg_prevent_modify;
--   * enum'ы вынесены в CREATE TYPE; при развитии значений — ALTER TYPE ... ADD VALUE.
-- Значения enum, не заданные каноном явно (например, статусы batch/report), помечены
-- комментарием "-- TODO(confirm)" и подлежат подтверждению перед M0.
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy-поиск по alias/наименованиям
CREATE EXTENSION IF NOT EXISTS citext;     -- регистронезависимый email

-- ---------------------------------------------------------------------
-- 0. Общие функции
-- ---------------------------------------------------------------------

-- Запрет UPDATE/DELETE для immutable/append-only таблиц.
CREATE OR REPLACE FUNCTION trg_prevent_modify() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Таблица % неизменяема (%).', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

-- Автоматическое обновление updated_at.
CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------
-- 1. Перечисления (enum)
-- ---------------------------------------------------------------------

-- viewer добавлен по факту реализации (таблица roles рабочей БД); его права ещё не
-- описаны в модели RBAC — открытый вопрос DB-07.
CREATE TYPE user_role            AS ENUM ('manager', 'admin', 'sysadmin', 'viewer');
CREATE TYPE user_status          AS ENUM ('INVITED', 'ACTIVE', 'DISABLED', 'LOCKED');

CREATE TYPE check_batch_status   AS ENUM ('DRAFT', 'QUEUED', 'RUNNING', 'REVIEW_REQUIRED',
                                          'COMPLETED', 'CANCELLED', 'ARCHIVED'); -- TODO(confirm)
CREATE TYPE check_item_status    AS ENUM (
  'DRAFT', 'QUEUED', 'INGESTING', 'NEEDS_INPUT', 'EXTRACTING', 'VERIFYING_SOURCES',
  'NORMALIZING', 'AWAITING_PARAMETER_REVIEW', 'MATCHING', 'REVIEW_REQUIRED',
  'READY_FOR_MANAGER_REVIEW', 'VERDICT_APPROVED', 'REPORTING', 'COMPLETED',
  'FAILED', 'CANCELLED');

CREATE TYPE pipeline_stage       AS ENUM ('INGESTING', 'EXTRACTING', 'VERIFYING_SOURCES',
                                          'NORMALIZING', 'MATCHING', 'REPORTING');
CREATE TYPE stage_status         AS ENUM ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');
CREATE TYPE pipeline_run_status  AS ENUM ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');

CREATE TYPE document_role        AS ENUM ('REQUIREMENT', 'FACT');
CREATE TYPE document_type        AS ENUM ('NATIVE', 'SCANNED', 'MIXED', 'CORRUPTED');
CREATE TYPE document_status      AS ENUM ('UPLOADED', 'QUARANTINE', 'READY', 'REJECTED');
CREATE TYPE document_source      AS ENUM ('UPLOAD', 'BITRIX', 'API');
CREATE TYPE locator_kind         AS ENUM ('PDF', 'XLSX', 'DOCX', 'MANUAL');
CREATE TYPE fragment_method      AS ENUM ('TABLE', 'TEXT', 'OCR', 'COMPOSITE');

CREATE TYPE extraction_method    AS ENUM ('TABLE', 'TEMPLATE', 'REGEX', 'LLM', 'MANUAL');
CREATE TYPE verification_method  AS ENUM ('EXACT_SUBSTRING', 'NORMALIZED', 'FUZZY', 'MANUAL');
CREATE TYPE value_kind           AS ENUM ('NUMBER', 'RANGE', 'TEXT', 'BOOLEAN', 'SET',
                                          'MATERIAL', 'UNKNOWN');

CREATE TYPE catalog_status       AS ENUM ('DRAFT', 'VALIDATING', 'VALID', 'INVALID',
                                          'PUBLISHED', 'RETIRED');
CREATE TYPE suggestion_status    AS ENUM ('PENDING', 'ACCEPTED', 'REJECTED');
CREATE TYPE issue_severity       AS ENUM ('INFO', 'WARNING', 'ERROR');
CREATE TYPE conversion_formula   AS ENUM ('LINEAR');

CREATE TYPE match_method         AS ENUM ('CANONICAL', 'ALIAS', 'FUZZY', 'EMBEDDING', 'MANUAL');
CREATE TYPE comparison_operator  AS ENUM ('EQUAL', 'NOT_EQUAL', 'GREATER', 'GREATER_OR_EQUAL',
                                          'LESS', 'LESS_OR_EQUAL', 'RANGE', 'SET_CONTAINS',
                                          'TEXT_MATCH', 'MATERIAL_MATCH');
CREATE TYPE comparison_status    AS ENUM ('COMPLIANT', 'NON_COMPLIANT', 'REVIEW_REQUIRED',
                                          'NOT_FOUND_IN_FACT', 'NOT_REQUIRED');
CREATE TYPE row_kind             AS ENUM ('PAIRED', 'MISSING_FACT', 'EXTRA_FACT');
CREATE TYPE verdict_value        AS ENUM ('COMPLIANT', 'PARTIAL', 'NON_COMPLIANT', 'PENDING');

CREATE TYPE prompt_status        AS ENUM ('DRAFT', 'ACTIVE', 'DEPRECATED');

CREATE TYPE report_status        AS ENUM ('QUEUED', 'GENERATING', 'COMPLETED',
                                          'PARTIALLY_COMPLETED', 'FAILED');
CREATE TYPE report_format        AS ENUM ('XLSX', 'PDF');

CREATE TYPE integration_job_type   AS ENUM ('IMPORT_FILE', 'PUBLISH_REPORT');
CREATE TYPE integration_job_status AS ENUM ('QUEUED', 'RUNNING', 'RETRY_SCHEDULED',
                                            'COMPLETED', 'FAILED', 'CANCELLED');

-- =====================================================================
-- 2. identity — пользователи и сессии
-- =====================================================================

CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         CITEXT      NOT NULL,
  display_name  TEXT        NOT NULL,
  password_hash TEXT,                          -- Argon2id; NULL для invited до установки
  external_subject TEXT,                       -- sub внешнего IdP (SSO); альтернатива password_hash
  status        user_status NOT NULL DEFAULT 'INVITED',
  version       INTEGER     NOT NULL DEFAULT 1,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_users_email UNIQUE (email)
);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
CREATE UNIQUE INDEX uq_users_external_subject ON users (external_subject)
  WHERE external_subject IS NOT NULL;
COMMENT ON TABLE users IS 'Учётные записи; email уникален, пароль только как Argon2id hash.';
-- organization_id (SYSTEM_ARCHITECTURE.md §9.1) не добавлен: multi-tenancy — открытый вопрос
-- заказчику (§1016 п.8). До ответа организация хранится в tender_refs.organization.

CREATE TABLE user_roles (
  id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id  UUID      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role     user_role NOT NULL,
  CONSTRAINT uq_user_roles UNIQUE (user_id, role)
);
COMMENT ON TABLE user_roles IS 'Роли пользователя (manager/admin/sysadmin).';

CREATE TABLE refresh_sessions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash     TEXT        NOT NULL,
  family_id      UUID        NOT NULL,          -- token family для reuse-detection
  issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL,
  revoked_at     TIMESTAMPTZ,
  CONSTRAINT uq_refresh_token UNIQUE (token_hash)
);
CREATE INDEX ix_refresh_user   ON refresh_sessions (user_id);
CREATE INDEX ix_refresh_family ON refresh_sessions (family_id);
COMMENT ON TABLE refresh_sessions IS 'Refresh-сессии с ротацией; хранится только hash токена.';

-- =====================================================================
-- 3. tenders
-- =====================================================================

CREATE TABLE tender_refs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization  TEXT        NOT NULL,           -- single-tenant, но храним явно
  tender_id     TEXT        NOT NULL,           -- внешний id/сделка Bitrix; НЕ внутренний PK
  bitrix_deal_id TEXT,
  title         TEXT,
  customer      TEXT,
  metadata      JSONB,                          -- даты, статус сделки и прочий контекст из Bitrix (§9.1)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_tender UNIQUE (organization, tender_id)
);
COMMENT ON TABLE tender_refs IS 'Локальная ссылка на сделку/тендер; внешний tender_id не PK. Единственное представление тендера: таблицы tenders/customers витрины не используются.';

-- =====================================================================
-- 4. catalog — версионируемые справочники
-- =====================================================================

CREATE TABLE catalog_versions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code          TEXT        NOT NULL,           -- напр. 'catalog-1.0.0'
  status        catalog_status NOT NULL DEFAULT 'DRAFT',
  is_default    BOOLEAN     NOT NULL DEFAULT false,  -- единственная published-по-умолчанию
  parent_id     UUID        REFERENCES catalog_versions(id),
  validation    JSONB,                          -- errors/warnings/regression_summary
  version       INTEGER     NOT NULL DEFAULT 1,
  created_by    UUID        REFERENCES users(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at  TIMESTAMPTZ,
  retired_at    TIMESTAMPTZ,
  CONSTRAINT uq_catalog_code UNIQUE (code)
);
-- Только одна default-published версия одновременно.
CREATE UNIQUE INDEX uq_catalog_single_default ON catalog_versions (is_default)
  WHERE is_default;
COMMENT ON TABLE catalog_versions IS 'Версия справочников; published неизменяема, правки — новый draft.';

CREATE TABLE canonical_parameters (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID        NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  code               TEXT        NOT NULL,      -- напр. 'FLOW_RATE'
  display_name       TEXT        NOT NULL,
  value_kind         value_kind  NOT NULL,
  dimension          TEXT,                      -- напр. 'VOLUMETRIC_FLOW'
  canonical_unit_id  UUID,                      -- FK на units той же версии каталога (см. ниже)
  active             BOOLEAN     NOT NULL DEFAULT true,
  CONSTRAINT uq_canon_param UNIQUE (catalog_version_id, code)
);
COMMENT ON TABLE canonical_parameters IS 'Канонические параметры внутри версии каталога.';

CREATE TABLE parameter_aliases (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID    NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  canonical_parameter_id UUID NOT NULL REFERENCES canonical_parameters(id) ON DELETE CASCADE,
  alias_text         TEXT    NOT NULL,
  normalized_text    TEXT    NOT NULL,          -- нормализованная форма для сравнения
  scope              TEXT    NOT NULL DEFAULT 'GLOBAL', -- GLOBAL | equipment_type_id
  priority           INTEGER NOT NULL DEFAULT 100,
  active             BOOLEAN NOT NULL DEFAULT true,
  -- Два активных alias с одинаковым normalized+scope не должны вести к разным параметрам.
  CONSTRAINT uq_alias_norm_scope UNIQUE (catalog_version_id, normalized_text, scope)
);
CREATE INDEX ix_alias_trgm ON parameter_aliases USING gin (normalized_text gin_trgm_ops);
COMMENT ON TABLE parameter_aliases IS 'Синонимы канонических параметров с scope и priority.';

CREATE TABLE catalog_suggestions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_text     TEXT             NOT NULL,
  synonym_text       TEXT             NOT NULL,
  canonical_parameter_id UUID         REFERENCES canonical_parameters(id),
  author_id          UUID             REFERENCES users(id),
  similarity         NUMERIC(6,4),
  status             suggestion_status NOT NULL DEFAULT 'PENDING',
  created_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),
  decided_at         TIMESTAMPTZ,
  decided_by         UUID             REFERENCES users(id)
);
CREATE INDEX ix_suggestion_status ON catalog_suggestions (status);
COMMENT ON TABLE catalog_suggestions IS 'Предложения синонимов от менеджеров; не меняют published каталог.';

CREATE TABLE units (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  code               TEXT NOT NULL,             -- напр. 'M3_PER_HOUR'
  symbol             TEXT NOT NULL,             -- напр. 'м³/ч'
  dimension          TEXT NOT NULL,
  active             BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT uq_unit_code UNIQUE (catalog_version_id, code),
  -- Цель составного ключа — ссылаться на единицу вместе с версией каталога.
  CONSTRAINT uq_units_version_id UNIQUE (catalog_version_id, id)
);
COMMENT ON TABLE units IS 'Единицы измерения внутри версии каталога.';

-- Единица канонического параметра обязана принадлежать той же версии каталога.
ALTER TABLE canonical_parameters
  ADD CONSTRAINT fk_canon_unit
  FOREIGN KEY (catalog_version_id, canonical_unit_id)
  REFERENCES units (catalog_version_id, id);

CREATE TABLE unit_aliases (
  id                 UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID    NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  unit_id            UUID    NOT NULL,
  alias_text         TEXT    NOT NULL,          -- как встречается в документе: 'куб.м/час'
  normalized_text    TEXT    NOT NULL,          -- нормализованная форма для сравнения
  priority           INTEGER NOT NULL DEFAULT 100,
  active             BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT uq_unit_alias UNIQUE (catalog_version_id, normalized_text),
  CONSTRAINT fk_unit_alias_unit FOREIGN KEY (catalog_version_id, unit_id)
    REFERENCES units (catalog_version_id, id) ON DELETE CASCADE
);
-- Fuzzy-поиск по OCR-искажениям ('мз/ч' вместо 'м3/ч'), как у parameter_aliases.
CREATE INDEX ix_unit_alias_trgm ON unit_aliases USING gin (normalized_text gin_trgm_ops);
COMMENT ON TABLE unit_aliases IS 'Синонимы единиц измерения внутри версии каталога; механика совпадает с parameter_aliases.';

CREATE TABLE conversion_rules (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  from_unit_code     TEXT NOT NULL,
  to_unit_code       TEXT NOT NULL,
  dimension          TEXT NOT NULL,
  formula_type       conversion_formula NOT NULL DEFAULT 'LINEAR',
  scale              NUMERIC NOT NULL,          -- Decimal, без произвольных формул
  "offset"           NUMERIC NOT NULL DEFAULT 0,
  CONSTRAINT uq_conversion UNIQUE (catalog_version_id, from_unit_code, to_unit_code)
);
COMMENT ON TABLE conversion_rules IS 'Линейные конверсии единиц (scale/offset, Decimal).';

CREATE TABLE material_nodes (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  parent_id          UUID REFERENCES material_nodes(id),
  code               TEXT NOT NULL,
  path               TEXT NOT NULL,             -- материализованный путь; backend запрещает циклы
  name               TEXT NOT NULL,
  active             BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT uq_material_code UNIQUE (catalog_version_id, code),
  CONSTRAINT uq_material_path UNIQUE (catalog_version_id, path)
);
COMMENT ON TABLE material_nodes IS 'Иерархия материалов; без циклов и дублей code/path.';

CREATE TABLE equipment_types (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  parent_id          UUID REFERENCES equipment_types(id),
  code               TEXT NOT NULL,
  name               TEXT NOT NULL,
  classifier_labels  JSONB,                     -- {keywords, weights, ...}: вход классификатора типа
  active             BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT uq_equipment_code UNIQUE (catalog_version_id, code)
);
COMMENT ON TABLE equipment_types IS 'Типы оборудования (иерархия) внутри версии каталога.';
COMMENT ON COLUMN equipment_types.classifier_labels IS 'Метки/правила для классификатора, заполняющего check_items.proposed_type_id.';

CREATE TABLE parameter_templates (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version_id UUID    NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
  equipment_type_id  UUID    NOT NULL REFERENCES equipment_types(id) ON DELETE CASCADE,
  canonical_parameter_id UUID NOT NULL REFERENCES canonical_parameters(id),
  required           BOOLEAN NOT NULL DEFAULT false,
  operator           comparison_operator,
  tolerance          JSONB,
  priority           INTEGER NOT NULL DEFAULT 100,
  CONSTRAINT uq_template UNIQUE (equipment_type_id, canonical_parameter_id, catalog_version_id)
);
COMMENT ON TABLE parameter_templates IS 'Шаблон ТХ типа оборудования: параметр, required, operator, tolerance.';

-- =====================================================================
-- 5. ai_gateway
-- =====================================================================

CREATE TABLE prompt_registry (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code          TEXT        NOT NULL,
  version       TEXT        NOT NULL,
  status        prompt_status NOT NULL DEFAULT 'DRAFT',
  body          TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_prompt UNIQUE (code, version)
);
COMMENT ON TABLE prompt_registry IS 'Промпты как версионируемые объекты (DRAFT/ACTIVE/DEPRECATED).';

CREATE TABLE ai_run_configs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider       TEXT        NOT NULL,
  model          TEXT        NOT NULL,
  prompt_id      UUID        REFERENCES prompt_registry(id),
  prompt_version TEXT,
  temperature    NUMERIC(4,3),
  params         JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ai_run_configs IS 'Неизменяемая конфигурация AI-запуска (provider/model/prompt/temperature).';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON ai_run_configs
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

-- =====================================================================
-- 6. pipeline definitions и снимки правил
-- =====================================================================

CREATE TABLE pipeline_definitions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  version       TEXT        NOT NULL,
  fsm           JSONB       NOT NULL,           -- stages + transitions
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_pipeline_def UNIQUE (version)
);
COMMENT ON TABLE pipeline_definitions IS 'Неизменяемая версия FSM pipeline.';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON pipeline_definitions
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

CREATE TABLE rule_snapshots (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_version  TEXT        NOT NULL,
  rules         JSONB       NOT NULL,           -- полный набор правил сравнения на момент прогона
  checksum      CHAR(64)    NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Один и тот же набор правил переиспользуется прогонами без дублирования JSONB.
  CONSTRAINT uq_rule_snapshot UNIQUE (rule_version, checksum)
);
COMMENT ON TABLE rule_snapshots IS 'Неизменяемый снимок правил сравнения; на него ссылается pipeline_runs для воспроизводимости статуса.';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON rule_snapshots
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

-- =====================================================================
-- 7. checks — пакеты и позиции
-- =====================================================================

CREATE TABLE check_batches (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tender_ref_id UUID        REFERENCES tender_refs(id),
  title         TEXT,
  customer      TEXT,
  supplier      TEXT,
  status        check_batch_status NOT NULL DEFAULT 'DRAFT',
  final_verdict verdict_value NOT NULL DEFAULT 'PENDING',
  author_id     UUID        NOT NULL REFERENCES users(id),
  version       INTEGER     NOT NULL DEFAULT 1,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ
);
CREATE INDEX ix_batch_status ON check_batches (status);
CREATE INDEX ix_batch_tender ON check_batches (tender_ref_id);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON check_batches
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
COMMENT ON TABLE check_batches IS 'Пакет проверки сделки (1..5 позиций); optimistic lock через version.';
COMMENT ON COLUMN check_batches.final_verdict IS 'Производное от item_verdicts позиций; пересчитывает модуль verdicts при любом изменении вердикта позиции.';

CREATE TABLE check_items (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id           UUID        NOT NULL REFERENCES check_batches(id) ON DELETE CASCADE,
  position_no        INTEGER     NOT NULL,
  name               TEXT        NOT NULL,
  status             check_item_status NOT NULL DEFAULT 'DRAFT',
  equipment_type_id  UUID        REFERENCES equipment_types(id),
  proposed_type_id   UUID        REFERENCES equipment_types(id),
  type_confidence    NUMERIC(5,4),
  supplier           TEXT,                      -- одна позиция = один поставщик (scope v1)
  version            INTEGER     NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_item_position UNIQUE (batch_id, position_no),
  CONSTRAINT ck_item_position_range CHECK (position_no BETWEEN 1 AND 5)
);
CREATE INDEX ix_item_status ON check_items (status);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON check_items
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
COMMENT ON TABLE check_items IS 'Позиция = пара «ТЗ ↔ документ одного поставщика»; до 5 на пакет.';
COMMENT ON COLUMN check_items.proposed_type_id IS 'Тип, предложенный классификатором; equipment_type_id — подтверждённый менеджером.';
COMMENT ON COLUMN check_items.type_confidence IS 'Уверенность классификатора в proposed_type_id (0..1); ниже порога — требуется подтверждение.';

CREATE TABLE pipeline_runs (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id        UUID        NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  run_no               INTEGER     NOT NULL,
  status               pipeline_run_status NOT NULL DEFAULT 'QUEUED',
  pipeline_definition_id UUID      NOT NULL REFERENCES pipeline_definitions(id),
  ai_run_config_id     UUID        REFERENCES ai_run_configs(id),
  rule_snapshot_id     UUID        REFERENCES rule_snapshots(id),  -- версия правил сравнения (rules module)
  catalog_version_id   UUID        REFERENCES catalog_versions(id),
  started_at           TIMESTAMPTZ,
  finished_at          TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_run_no UNIQUE (check_item_id, run_no)
);
CREATE INDEX ix_run_item ON pipeline_runs (check_item_id);
COMMENT ON TABLE pipeline_runs IS 'Воспроизводимый запуск позиции с зафиксированными версиями pipeline/AI/rules/catalog.';

CREATE TABLE stage_runs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_run_id UUID       NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  stage          pipeline_stage NOT NULL,
  attempt        INTEGER     NOT NULL DEFAULT 1,
  status         stage_status NOT NULL DEFAULT 'QUEUED',
  progress_current INTEGER,
  progress_total   INTEGER,
  error_code     TEXT,                          -- плоский код для агрегации и алертов
  error          JSONB,                         -- {code, message, retryable}; code дублирует error_code
  metrics        JSONB,                         -- {pages, ocr_pages, duration_ms, ...}
  started_at     TIMESTAMPTZ,
  finished_at    TIMESTAMPTZ,
  CONSTRAINT uq_stage_attempt UNIQUE (pipeline_run_id, stage, attempt)
);
COMMENT ON TABLE stage_runs IS 'Состояние отдельного этапа pipeline с попытками и прогрессом.';
COMMENT ON COLUMN stage_runs.error_code IS 'Дублирует error->>''code''; синхронность поддерживает сервис pipeline.';

CREATE TABLE ai_usage_log (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_run_id UUID       REFERENCES pipeline_runs(id) ON DELETE SET NULL,
  ai_run_config_id UUID      REFERENCES ai_run_configs(id),
  prompt_id      UUID        REFERENCES prompt_registry(id),
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  cost_usd       NUMERIC(12,6),
  latency_ms     INTEGER,
  input_hash     TEXT,
  correlation_id UUID,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_usage_run ON ai_usage_log (pipeline_run_id);
COMMENT ON TABLE ai_usage_log IS 'Учёт AI-вызовов: токены, стоимость, латентность, без секретов и текста документа.';

-- =====================================================================
-- 8. documents — документы и происхождение
-- =====================================================================

CREATE TABLE documents (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id      UUID        NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  role               document_role NOT NULL,
  active_version_id  UUID,                      -- FK добавляется ниже (циклическая ссылка)
  profile            JSONB,                     -- document fingerprint: document_type, ocr_needed
  source             document_source NOT NULL DEFAULT 'UPLOAD',
  supplier_id        TEXT,                      -- для fact-документов «на будущее»
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_documents_item ON documents (check_item_id, role);
COMMENT ON TABLE documents IS 'Логический документ (requirement/fact) с профилем и активной версией.';

CREATE TABLE document_versions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id   UUID        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_no    INTEGER     NOT NULL DEFAULT 1,
  file_name     TEXT        NOT NULL,           -- только metadata, не storage path
  media_type    TEXT        NOT NULL,
  size_bytes    BIGINT      NOT NULL,
  sha256        CHAR(64)    NOT NULL,
  storage_key   TEXT        NOT NULL,           -- не содержит пользовательского имени
  status        document_status NOT NULL DEFAULT 'UPLOADED',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_docver UNIQUE (document_id, version_no),
  CONSTRAINT uq_docver_sha UNIQUE (document_id, sha256)
);
COMMENT ON TABLE document_versions IS 'Неизменяемая версия файла (SHA-256, storage key).';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON document_versions
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

-- Циклическая ссылка documents.active_version_id -> document_versions.id
ALTER TABLE documents
  ADD CONSTRAINT fk_documents_active_version
  FOREIGN KEY (active_version_id) REFERENCES document_versions(id);

CREATE TABLE document_pages (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_version_id UUID       NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  page_no            INTEGER     NOT NULL,
  dpi                INTEGER,
  width_px           INTEGER,                   -- размеры растра при указанном dpi;
  height_px          INTEGER,                   -- база для пересчёта bbox при подсветке в UI
  text_hash          TEXT,                      -- хеш текста страницы: детекция изменений при перепарсинге
  document_type      document_type,             -- per-page решение об OCR
  ocr_needed         BOOLEAN     NOT NULL DEFAULT false,
  quality_score      NUMERIC(5,4),
  active_layer       TEXT,                      -- NATIVE | OCR + причина
  parser_version     TEXT,                      -- чем получена страница; document_versions immutable
  CONSTRAINT uq_page UNIQUE (document_version_id, page_no)
);
COMMENT ON TABLE document_pages IS 'Страница с DPI, качеством и признаком OCR; native/OCR layers раздельно.';

CREATE TABLE source_fragments (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_version_id UUID       NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  page_id            UUID        REFERENCES document_pages(id),
  method             fragment_method NOT NULL,
  fragment_raw_text  TEXT        NOT NULL,      -- как получил parser/OCR (immutable для аудита)
  normalized_text    TEXT,
  text_hash          TEXT        NOT NULL,
  locator_kind       locator_kind NOT NULL,
  locator            JSONB       NOT NULL,      -- SourceLocatorDTO: page/bbox/sheet/range/...
  parser_version     TEXT,                      -- версия парсера/OCR, породившего фрагмент
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_fragment_version ON source_fragments (document_version_id);
COMMENT ON TABLE source_fragments IS 'Фрагмент текста/таблицы с локатором для перехода и подсветки.';

CREATE TABLE document_tables (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_fragment_id UUID        NOT NULL REFERENCES source_fragments(id) ON DELETE CASCADE,
  structure          JSONB       NOT NULL,      -- восстановленные строки/столбцы/ячейки (TSR)
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE document_tables IS 'Восстановленная структура таблицы (Table Structure Recognition).';

-- =====================================================================
-- 9. extraction — извлечение
-- =====================================================================

CREATE TABLE extraction_runs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_run_id UUID       REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  check_item_id  UUID        NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  method         extraction_method NOT NULL,
  provider       TEXT,
  model          TEXT,
  prompt_version TEXT,
  usage          JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_extraction_item ON extraction_runs (check_item_id);
COMMENT ON TABLE extraction_runs IS 'Запуск извлечения (table/regex/OCR/LLM) с версиями алгоритма.';

CREATE TABLE raw_parameters (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id  UUID        NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  extraction_run_id UUID     REFERENCES extraction_runs(id) ON DELETE SET NULL,
  role           document_role NOT NULL,
  name_text      TEXT        NOT NULL,          -- имя как в источнике
  value_text     TEXT,                          -- значение как в источнике
  unit_text      TEXT,
  extraction_method extraction_method NOT NULL,
  confidence     NUMERIC(5,4),
  quality_flags  TEXT[]      NOT NULL DEFAULT '{}',  -- LOW_OCR_CONFIDENCE, AMBIGUOUS_UNIT, ...
  excluded       BOOLEAN     NOT NULL DEFAULT false,
  version        INTEGER     NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_raw_item_role ON raw_parameters (check_item_id, role);
COMMENT ON TABLE raw_parameters IS 'Параметр «как в источнике»; исходный текст хранится всегда.';

CREATE TABLE extraction_evidence (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_parameter_id UUID      NOT NULL REFERENCES raw_parameters(id) ON DELETE CASCADE,
  source_fragment_id UUID    NOT NULL REFERENCES source_fragments(id),
  quote          TEXT        NOT NULL,
  quote_start    INTEGER,
  quote_end      INTEGER,
  value_start    INTEGER,
  value_end      INTEGER,
  verified       BOOLEAN     NOT NULL DEFAULT false,  -- Source Verifier hard gate
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_evidence_param ON extraction_evidence (raw_parameter_id);
COMMENT ON TABLE extraction_evidence IS 'Evidence параметра: цитата и offsets; value_text ⊂ quote; verified.';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON extraction_evidence
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

CREATE TABLE source_verifications (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  extraction_evidence_id UUID NOT NULL REFERENCES extraction_evidence(id) ON DELETE CASCADE,
  passed         BOOLEAN     NOT NULL,
  method         verification_method NOT NULL DEFAULT 'EXACT_SUBSTRING',
  rejection_reason TEXT,
  verifier_version TEXT      NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE source_verifications IS 'Проверки перепривязки цитаты к источнику (hard gate).';
COMMENT ON COLUMN source_verifications.method IS 'Способ подтверждения: точное вхождение, нормализованное сравнение, fuzzy или ручное. Определяет уровень доверия к evidence.';

-- =====================================================================
-- 10. review — ручные правки
-- =====================================================================

CREATE TABLE parameter_revisions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_parameter_id UUID      NOT NULL REFERENCES raw_parameters(id) ON DELETE CASCADE,
  previous_revision_id UUID  REFERENCES parameter_revisions(id),
  name_text      TEXT,
  value_text     TEXT,
  unit_text      TEXT,
  reason_code    TEXT        NOT NULL,
  comment        TEXT        NOT NULL,
  actor_id       UUID        NOT NULL REFERENCES users(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_revision_param ON parameter_revisions (raw_parameter_id);
COMMENT ON TABLE parameter_revisions IS 'Ручная корректировка параметра; оригинал не удаляется.';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON parameter_revisions
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

-- =====================================================================
-- 11. normalization
-- =====================================================================

CREATE TABLE normalized_parameters (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_parameter_id   UUID    NOT NULL REFERENCES raw_parameters(id) ON DELETE CASCADE,
  catalog_version_id UUID    NOT NULL REFERENCES catalog_versions(id),
  canonical_parameter_id UUID REFERENCES canonical_parameters(id),
  canonical_name     TEXT,
  value_kind         value_kind NOT NULL,
  typed_value        JSONB,                     -- {kind, value, canonical_unit, ...}; Decimal как text
  canonical_unit_id  UUID,
  normalized_ok      BOOLEAN NOT NULL DEFAULT true,
  version            INTEGER NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_normalized_raw UNIQUE (raw_parameter_id),
  -- Единица нормализованного значения — из той же версии каталога.
  CONSTRAINT fk_norm_unit FOREIGN KEY (catalog_version_id, canonical_unit_id)
    REFERENCES units (catalog_version_id, id)
);
CREATE INDEX ix_norm_canonical ON normalized_parameters (canonical_parameter_id);
COMMENT ON TABLE normalized_parameters IS 'Канонический параметр и типизированное значение (Decimal через JSON).';

CREATE TABLE normalization_issues (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  normalized_parameter_id UUID NOT NULL REFERENCES normalized_parameters(id) ON DELETE CASCADE,
  issue_type              TEXT NOT NULL,        -- UNKNOWN_UNIT | AMBIGUOUS_ALIAS | RANGE_PARSE_FAILED | ...
  description             TEXT,
  severity                issue_severity NOT NULL DEFAULT 'WARNING',
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_norm_issue_param ON normalization_issues (normalized_parameter_id);
CREATE INDEX ix_norm_issue_type  ON normalization_issues (issue_type, severity);
COMMENT ON TABLE normalization_issues IS 'Проблемы нормализации значения; ортогональны статусу сравнения, как и quality_flags у raw_parameters.';

-- =====================================================================
-- 12. matching / rules / verdicts
-- =====================================================================

CREATE TABLE match_candidates (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id      UUID    NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  requirement_parameter_id UUID REFERENCES normalized_parameters(id),
  fact_parameter_id  UUID    REFERENCES normalized_parameters(id),
  method             match_method NOT NULL,
  score              NUMERIC(6,4),
  rank               INTEGER,
  eligible           BOOLEAN NOT NULL DEFAULT true,
  rejection_reasons  TEXT[]  NOT NULL DEFAULT '{}',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_match_item ON match_candidates (check_item_id);
COMMENT ON TABLE match_candidates IS 'Кандидаты пары requirement↔fact; fuzzy/embedding не выставляют статус.';

CREATE TABLE comparisons (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id      UUID    NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  pipeline_run_id    UUID    REFERENCES pipeline_runs(id),
  canonical_parameter_id UUID REFERENCES canonical_parameters(id),
  requirement_parameter_id UUID REFERENCES normalized_parameters(id),
  fact_parameter_id  UUID    REFERENCES normalized_parameters(id),
  row_kind           row_kind NOT NULL DEFAULT 'PAIRED',
  operator           comparison_operator,
  computed_status    comparison_status NOT NULL,
  effective_status   comparison_status NOT NULL,
  explanation        JSONB,                     -- required/fact/expression/result/rule_version
  lineage_chain      JSONB,                     -- происхождение значения по стадиям
  overridden         BOOLEAN NOT NULL DEFAULT false,
  stale              BOOLEAN NOT NULL DEFAULT false,
  version            INTEGER NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_comparison_item ON comparisons (check_item_id);
CREATE INDEX ix_comparison_status ON comparisons (effective_status);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON comparisons
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
COMMENT ON TABLE comparisons IS 'Результат сопоставления: computed vs effective статус, объяснение правила, lineage.';

CREATE TABLE comparison_overrides (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  comparison_id  UUID        NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
  old_status     comparison_status NOT NULL,
  new_status     comparison_status NOT NULL,
  reason_code    TEXT,
  comment        TEXT        NOT NULL,          -- обязателен
  actor_id       UUID        NOT NULL REFERENCES users(id),
  active         BOOLEAN     NOT NULL DEFAULT true,  -- отмена = новая запись/деактивация
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_override_comparison ON comparison_overrides (comparison_id);
COMMENT ON TABLE comparison_overrides IS 'Ручная смена статуса сравнения с обязательным комментарием; computed сохраняется.';

CREATE TABLE item_verdicts (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id      UUID        NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  computed_verdict   verdict_value NOT NULL,
  manager_verdict    verdict_value NOT NULL DEFAULT 'PENDING',
  basis              JSONB,                     -- распределение статусов сравнений
  comment            TEXT,                      -- обязателен при расхождении с computed
  approved_by        UUID        REFERENCES users(id),
  approved_at        TIMESTAMPTZ,
  version            INTEGER     NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_verdict_item UNIQUE (check_item_id)
);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON item_verdicts
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
COMMENT ON TABLE item_verdicts IS 'Вердикт позиции: computed vs manager; при расхождении комментарий обязателен.';

-- =====================================================================
-- 13. reporting
-- =====================================================================

CREATE TABLE report_snapshots (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_batch_id UUID        REFERENCES check_batches(id) ON DELETE CASCADE,
  check_item_id  UUID        REFERENCES check_items(id) ON DELETE CASCADE,
  status         report_status NOT NULL DEFAULT 'QUEUED',
  payload        JSONB       NOT NULL,          -- immutable набор данных отчёта
  checksum       CHAR(64)    NOT NULL,
  language       TEXT        NOT NULL DEFAULT 'ru',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_report_scope CHECK (check_batch_id IS NOT NULL OR check_item_id IS NOT NULL)
);
COMMENT ON TABLE report_snapshots IS 'Неизменяемый snapshot отчёта (payload/checksum).';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON report_snapshots
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

CREATE TABLE report_artifacts (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  report_snapshot_id UUID        NOT NULL REFERENCES report_snapshots(id) ON DELETE CASCADE,
  format             report_format NOT NULL,
  file_name          TEXT        NOT NULL,
  storage_key        TEXT        NOT NULL,
  size_bytes         BIGINT,
  checksum           CHAR(64),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_artifact_format UNIQUE (report_snapshot_id, format)
);
COMMENT ON TABLE report_artifacts IS 'Файлы отчёта XLSX/PDF с checksum и storage key.';

-- =====================================================================
-- 14. integrations
-- =====================================================================

CREATE TABLE external_links (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type    TEXT        NOT NULL,          -- 'check_batch' | 'document' | 'report_artifact'
  entity_id      UUID        NOT NULL,
  provider       TEXT        NOT NULL DEFAULT 'bitrix',
  external_entity_type TEXT,                    -- 'deal' | 'file' | 'task': различает одинаковые external_id
  external_id    TEXT        NOT NULL,
  metadata       JSONB,                         -- URL, имя папки и прочий контекст связи
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_external_link UNIQUE (provider, entity_type, entity_id, external_entity_type, external_id)
);
COMMENT ON TABLE external_links IS 'Связь локальной сущности с внешним ID (Bitrix).';

CREATE TABLE integration_jobs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type           integration_job_type NOT NULL,
  status         integration_job_status NOT NULL DEFAULT 'QUEUED',
  tender_id      TEXT,
  check_batch_id UUID        REFERENCES check_batches(id) ON DELETE SET NULL,
  attempt        INTEGER     NOT NULL DEFAULT 0,
  next_retry_at  TIMESTAMPTZ,
  request_meta   JSONB,                         -- параметры запроса без секретов и auth-заголовков
  result         JSONB,
  error          JSONB,                         -- {code, retryable}
  idempotency_key TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at   TIMESTAMPTZ,                   -- момент терминального статуса; updated_at меняется и при ретрае
  CONSTRAINT uq_integration_idem UNIQUE (idempotency_key)
);
CREATE INDEX ix_intjob_status ON integration_jobs (status);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON integration_jobs
  FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
COMMENT ON TABLE integration_jobs IS 'Состояние внешней операции (import/publish) с retry.';

CREATE TABLE integration_outbox (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type TEXT        NOT NULL,
  aggregate_id   UUID        NOT NULL,
  event_type     TEXT        NOT NULL,
  payload        JSONB       NOT NULL,
  idempotency_key TEXT       NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at   TIMESTAMPTZ,
  CONSTRAINT uq_outbox_idem UNIQUE (idempotency_key)
);
CREATE INDEX ix_outbox_unpublished ON integration_outbox (created_at) WHERE published_at IS NULL;
COMMENT ON TABLE integration_outbox IS 'Транзакционный outbox: доставка событий после DB commit.';

-- =====================================================================
-- 15. audit
-- =====================================================================

CREATE TABLE audit_events (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id       UUID        REFERENCES users(id),
  action         TEXT        NOT NULL,
  entity_type    TEXT        NOT NULL,
  entity_id      UUID,
  before         JSONB,                         -- без секретов и текста документа
  after          JSONB,
  reason         TEXT,
  correlation_id UUID,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_events (entity_type, entity_id);
CREATE INDEX ix_audit_actor  ON audit_events (actor_id);
COMMENT ON TABLE audit_events IS 'Append-only журнал действий; секреты и document text не хранятся.';
CREATE TRIGGER prevent_modify BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION trg_prevent_modify();

-- =====================================================================
-- 16. Граница с витриной Фазы 2
-- ---------------------------------------------------------------------
-- Товарный контур (products, product_attributes, suppliers, categories,
-- attributes, canonical_names, price_history, ...) живёт в собственной схеме
-- (db/schema_phase2_showcase.sql) и здесь НЕ определяется.
-- Правило: FK через границу контуров запрещён — витрина пересобирается
-- при перезаливке прайсов, и внешний ключ блокировал бы это.
-- =====================================================================

CREATE TABLE showcase_product_refs (
  id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  check_item_id       UUID    NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
  showcase_product_id BIGINT  NOT NULL,          -- products.id витрины; FK отсутствует намеренно
  matched_by          TEXT    NOT NULL,          -- MANUAL | CANONICAL_NAME | SEARCH
  confidence          NUMERIC(5,4),
  selected            BOOLEAN NOT NULL DEFAULT false,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_showcase_ref UNIQUE (check_item_id, showcase_product_id)
);
-- Выбранный товар у позиции может быть только один; остальные строки — кандидаты.
CREATE UNIQUE INDEX uq_showcase_selected ON showcase_product_refs (check_item_id)
  WHERE selected;
CREATE INDEX ix_showcase_product ON showcase_product_refs (showcase_product_id);
COMMENT ON TABLE showcase_product_refs IS
  'Кандидаты подбора товара витрины Ф2 для позиции проверки Ф3. Ссылочной целостности нет: витрина — внешний контур.';

COMMIT;

-- =====================================================================
-- Примечания к инвариантам (не выражаемым чистым DDL — уровень приложения):
--   * value_text ⊂ quote (extraction_evidence): проверяется Source Verifier до записи verified=true;
--   * у каждого автоизвлечённого параметра есть verified evidence: enforced в extraction stage;
--   * ручная правка создаёт parameter_revision И audit_event: транзакция сервиса review;
--   * status воспроизводим по rule_snapshot: pipeline_runs.rule_snapshot_id + immutable snapshots;
--   * quality_flags ортогональны статусу: массив флагов независим от effective_status.
-- Открытые значения (TODO(confirm)): полный набор статусов check_batch_status и
--   переходы batch-FSM — подтвердить по канону §6 перед фиксацией миграции M0.
-- =====================================================================
