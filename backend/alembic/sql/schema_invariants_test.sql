-- =====================================================================
-- Тесты инвариантов schema.sql (Фаза 3)
-- ---------------------------------------------------------------------
-- Проверяют, что ограничения БД действительно срабатывают, а не только
-- объявлены. Запуск на ЧИСТОЙ базе, куда уже применён schema.sql:
--
--   psql -d <db> -v ON_ERROR_STOP=1 -f schema.sql
--   psql -d <db> -v ON_ERROR_STOP=1 -f schema_invariants_test.sql
--
-- Скрипт полностью откатывается (ROLLBACK в конце) и данных не оставляет.
-- Падение любого теста прерывает выполнение с ненулевым кодом возврата.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Фикстуры
-- ---------------------------------------------------------------------

INSERT INTO catalog_versions (id, code) VALUES
  ('11111111-1111-1111-1111-111111111111', 'cat-1'),
  ('22222222-2222-2222-2222-222222222222', 'cat-2');

INSERT INTO units (id, catalog_version_id, code, symbol, dimension) VALUES
  ('a0000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
   'M3_PER_HOUR', 'м³/ч', 'VOLUMETRIC_FLOW'),
  ('a0000000-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222',
   'M3_PER_HOUR', 'м³/ч', 'VOLUMETRIC_FLOW');

INSERT INTO users (id, email, display_name) VALUES
  ('b0000000-0000-0000-0000-000000000001', 'manager@example.test', 'Менеджер');

INSERT INTO check_batches (id, author_id) VALUES
  ('c0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001');

INSERT INTO check_items (id, batch_id, position_no, name) VALUES
  ('d0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 1, 'Насос');

-- ---------------------------------------------------------------------
-- 1. Составной FK: единица должна быть из ТОЙ ЖЕ версии каталога (A1.2)
-- ---------------------------------------------------------------------

DO $$
BEGIN
  INSERT INTO canonical_parameters
    (catalog_version_id, code, display_name, value_kind, canonical_unit_id)
  VALUES
    ('11111111-1111-1111-1111-111111111111', 'FLOW', 'Расход', 'NUMBER',
     'a0000000-0000-0000-0000-000000000002');   -- единица из версии cat-2
  RAISE EXCEPTION 'FAIL 1: принята единица из чужой версии каталога';
EXCEPTION WHEN foreign_key_violation THEN
  RAISE NOTICE 'OK 1: единица чужой версии каталога отклонена';
END $$;

-- Позитивный случай: та же версия каталога — вставка проходит.
INSERT INTO canonical_parameters
  (id, catalog_version_id, code, display_name, value_kind, canonical_unit_id)
VALUES
  ('e0000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
   'FLOW', 'Расход', 'NUMBER', 'a0000000-0000-0000-0000-000000000001');

-- ---------------------------------------------------------------------
-- 2. rule_snapshots неизменяем (A1.1)
-- ---------------------------------------------------------------------

INSERT INTO rule_snapshots (id, rule_version, rules, checksum) VALUES
  ('f0000000-0000-0000-0000-000000000001', 'rules-1.0', '{"op":"GTE"}'::jsonb,
   repeat('a', 64));

DO $$
BEGIN
  UPDATE rule_snapshots SET rule_version = 'rules-1.1'
    WHERE id = 'f0000000-0000-0000-0000-000000000001';
  RAISE EXCEPTION 'FAIL 2a: снимок правил удалось изменить';
EXCEPTION WHEN restrict_violation THEN
  RAISE NOTICE 'OK 2a: UPDATE снимка правил отклонён';
END $$;

DO $$
BEGIN
  DELETE FROM rule_snapshots WHERE id = 'f0000000-0000-0000-0000-000000000001';
  RAISE EXCEPTION 'FAIL 2b: снимок правил удалось удалить';
EXCEPTION WHEN restrict_violation THEN
  RAISE NOTICE 'OK 2b: DELETE снимка правил отклонён';
END $$;

-- ---------------------------------------------------------------------
-- 3. Позиция проверки: не более одного выбранного товара витрины (A3.1)
-- ---------------------------------------------------------------------

INSERT INTO showcase_product_refs (check_item_id, showcase_product_id, matched_by, selected)
VALUES ('d0000000-0000-0000-0000-000000000001', 2345, 'SEARCH', true);

DO $$
BEGIN
  INSERT INTO showcase_product_refs (check_item_id, showcase_product_id, matched_by, selected)
  VALUES ('d0000000-0000-0000-0000-000000000001', 2346, 'MANUAL', true);
  RAISE EXCEPTION 'FAIL 3: у позиции оказалось два выбранных товара';
EXCEPTION WHEN unique_violation THEN
  RAISE NOTICE 'OK 3: второй selected-товар отклонён';
END $$;

-- Кандидаты (selected = false) ограничением не затронуты.
INSERT INTO showcase_product_refs (check_item_id, showcase_product_id, matched_by, selected)
VALUES ('d0000000-0000-0000-0000-000000000001', 2347, 'CANONICAL_NAME', false),
       ('d0000000-0000-0000-0000-000000000001', 2348, 'SEARCH', false);

-- ---------------------------------------------------------------------
-- 4. unit_aliases: нормализованный алиас уникален внутри версии каталога (A2.3)
-- ---------------------------------------------------------------------

INSERT INTO unit_aliases (catalog_version_id, unit_id, alias_text, normalized_text)
VALUES ('11111111-1111-1111-1111-111111111111', 'a0000000-0000-0000-0000-000000000001',
        'куб.м/час', 'м3/ч');

DO $$
BEGIN
  INSERT INTO unit_aliases (catalog_version_id, unit_id, alias_text, normalized_text)
  VALUES ('11111111-1111-1111-1111-111111111111', 'a0000000-0000-0000-0000-000000000001',
          'м³/ч', 'м3/ч');
  RAISE EXCEPTION 'FAIL 4a: дубль нормализованного алиаса принят';
EXCEPTION WHEN unique_violation THEN
  RAISE NOTICE 'OK 4a: дубль алиаса внутри версии каталога отклонён';
END $$;

-- Тот же алиас в другой версии каталога допустим.
INSERT INTO unit_aliases (catalog_version_id, unit_id, alias_text, normalized_text)
VALUES ('22222222-2222-2222-2222-222222222222', 'a0000000-0000-0000-0000-000000000002',
        'м³/ч', 'м3/ч');

DO $$
DECLARE n INTEGER;
BEGIN
  SELECT count(*) INTO n FROM unit_aliases WHERE normalized_text = 'м3/ч';
  IF n <> 2 THEN
    RAISE EXCEPTION 'FAIL 4b: ожидалось 2 алиаса в разных версиях, получено %', n;
  END IF;
  RAISE NOTICE 'OK 4b: одинаковый алиас в разных версиях каталога сосуществует';
END $$;

-- ---------------------------------------------------------------------
-- 5. users.external_subject: уникален, но допускает множественный NULL (A2.12)
-- ---------------------------------------------------------------------

INSERT INTO users (email, display_name, external_subject) VALUES
  ('sso1@example.test', 'SSO 1', 'idp|abc'),
  ('local1@example.test', 'Локальный 1', NULL),
  ('local2@example.test', 'Локальный 2', NULL);   -- два NULL допустимы

DO $$
BEGIN
  INSERT INTO users (email, display_name, external_subject)
  VALUES ('sso2@example.test', 'SSO 2', 'idp|abc');
  RAISE EXCEPTION 'FAIL 5: дубль external_subject принят';
EXCEPTION WHEN unique_violation THEN
  RAISE NOTICE 'OK 5: дубль external_subject отклонён, NULL не ограничен';
END $$;

-- ---------------------------------------------------------------------
-- 6. Автоопределение типа: proposed_type_id ссылается на equipment_types (A2.1)
-- ---------------------------------------------------------------------

INSERT INTO equipment_types (id, catalog_version_id, code, name, classifier_labels) VALUES
  ('a1000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
   'PUMP', 'Насос', '{"keywords": ["насос", "помпа"]}'::jsonb);

UPDATE check_items
   SET proposed_type_id = 'a1000000-0000-0000-0000-000000000001',
       type_confidence  = 0.8231
 WHERE id = 'd0000000-0000-0000-0000-000000000001';

DO $$
BEGIN
  UPDATE check_items
     SET proposed_type_id = 'a1000000-0000-0000-0000-0000000000ff'   -- несуществующий тип
   WHERE id = 'd0000000-0000-0000-0000-000000000001';
  RAISE EXCEPTION 'FAIL 6: принята ссылка на несуществующий тип оборудования';
EXCEPTION WHEN foreign_key_violation THEN
  RAISE NOTICE 'OK 6: ссылка на несуществующий тип оборудования отклонена';
END $$;

-- ---------------------------------------------------------------------
-- 7. Существующие инварианты не сломаны правками
-- ---------------------------------------------------------------------

DO $$
BEGIN
  INSERT INTO check_items (batch_id, position_no, name)
  VALUES ('c0000000-0000-0000-0000-000000000001', 6, 'Шестая позиция');
  RAISE EXCEPTION 'FAIL 7a: принята позиция с номером вне диапазона 1..5';
EXCEPTION WHEN check_violation THEN
  RAISE NOTICE 'OK 7a: ограничение 1..5 позиций работает';
END $$;

DO $$
DECLARE v verdict_value;
BEGIN
  SELECT final_verdict INTO v FROM check_batches
   WHERE id = 'c0000000-0000-0000-0000-000000000001';
  IF v <> 'PENDING' THEN
    RAISE EXCEPTION 'FAIL 7b: ожидался вердикт пакета PENDING, получен %', v;
  END IF;
  RAISE NOTICE 'OK 7b: вердикт пакета по умолчанию PENDING';
END $$;

-- now() возвращает время начала транзакции, поэтому «было раньше / стало позже»
-- внутри одного BEGIN проверить нельзя. Вместо этого подсовываем триггеру заведомо
-- старую дату: он обязан её перезаписать значением now().
DO $$
DECLARE t TIMESTAMPTZ;
BEGIN
  UPDATE check_items
     SET supplier = 'ООО Поставщик', updated_at = TIMESTAMPTZ '2000-01-01 00:00:00+00'
   WHERE id = 'd0000000-0000-0000-0000-000000000001';
  SELECT updated_at INTO t FROM check_items
   WHERE id = 'd0000000-0000-0000-0000-000000000001';
  IF t = TIMESTAMPTZ '2000-01-01 00:00:00+00' THEN
    RAISE EXCEPTION 'FAIL 7c: триггер не перезаписал updated_at';
  END IF;
  IF t <> now() THEN
    RAISE EXCEPTION 'FAIL 7c: updated_at = %, ожидалось now() = %', t, now();
  END IF;
  RAISE NOTICE 'OK 7c: триггер updated_at перезаписывает переданное значение';
END $$;

ROLLBACK;
