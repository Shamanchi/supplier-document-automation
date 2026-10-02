import { dimensionLabel, valueKindLabel } from "@/pages/dictionary/labels";
import type { DictionaryData, MaterialNode } from "@/shared/model/types";

/**
 * Поиск, фильтры и сортировка словаря.
 *
 * Считается на клиенте, потому что `GET /dictionary` и так отдаёт всю
 * опубликованную версию одним ответом: серверные параметры добавили бы
 * round-trip на каждое нажатие клавиши, ничего не выиграв. Когда появится
 * серверная пагинация (пункт 8 в docs/todo/dictionary.md), фильтры переедут
 * туда вместе с ней — эти функции описывают, что именно должно переехать.
 */

type Nomenclature = DictionaryData["nomenclature"][number];
type Unit = DictionaryData["units"][number];

/** Порядок сортировки колонки; `null` — сортировка не выбрана. */
export type SortDirection = "asc" | "desc";

export interface Sort<TColumn extends string> {
  column: TColumn;
  direction: SortDirection;
}

export type NomenclatureColumn = "canonical" | "code" | "valueKind" | "unit";
export type UnitColumn = "symbol" | "code" | "dimension";

export interface NomenclatureFilters {
  query: string;
  valueKind: string;
  dimension: string;
  /** `with` — только с единицей, `without` — только без неё. */
  unit: string;
}

export const ANY = "all";

export const EMPTY_NOMENCLATURE_FILTERS: NomenclatureFilters = {
  query: "",
  valueKind: ANY,
  dimension: ANY,
  unit: ANY,
};

/** Регистронезависимое сравнение с учётом кириллицы. */
function normalize(value: string) {
  return value.trim().toLocaleLowerCase("ru-RU");
}

function contains(haystack: Array<string | null | undefined>, query: string) {
  const needle = normalize(query);
  if (!needle) return true;
  return haystack.some((value) => value && normalize(value).includes(needle));
}

/** Сравнение строк по правилам русского алфавита, пустое значение — в конец. */
function compare(left: string | null, right: string | null) {
  if (left === right) return 0;
  if (!left) return 1;
  if (!right) return -1;
  return left.localeCompare(right, "ru-RU");
}

function ordered<T>(items: T[], value: (item: T) => string | null, direction: SortDirection) {
  const sorted = [...items].sort((a, b) => compare(value(a), value(b)));
  return direction === "asc" ? sorted : sorted.reverse();
}

/** Значения фильтра, собранные из самих данных: пустых вариантов не будет. */
export function optionsOf<T>(items: T[], value: (item: T) => string | null): string[] {
  return [...new Set(items.map(value).filter((item): item is string => Boolean(item)))].sort(
    (a, b) => a.localeCompare(b, "ru-RU"),
  );
}

export function filterNomenclature(
  items: Nomenclature[],
  filters: NomenclatureFilters,
): Nomenclature[] {
  return items.filter((item) => {
    // Синонимы участвуют в поиске наравне с названием: менеджер ищет словом,
    // которое встретил в документе, а оно чаще синоним, чем канон. Размерность
    // ищется и по коду, и по подписи — набирают «давление», а не PRESSURE.
    if (
      !contains(
        [
          item.canonical,
          item.code,
          dimensionLabel(item.dimension),
          item.dimension,
          ...item.synonyms,
        ],
        filters.query,
      )
    ) {
      return false;
    }
    if (filters.valueKind !== ANY && item.valueKind !== filters.valueKind) return false;
    if (filters.dimension !== ANY && item.dimension !== filters.dimension) return false;
    if (filters.unit === "with" && !item.unit) return false;
    if (filters.unit === "without" && item.unit) return false;
    return true;
  });
}

export function sortNomenclature(
  items: Nomenclature[],
  sort: Sort<NomenclatureColumn> | null,
): Nomenclature[] {
  if (!sort) return items;
  // Тип значения сортируется по подписи: видимый порядок должен совпадать с
  // алфавитным, а коды и подписи упорядочены по-разному.
  const value = (item: Nomenclature) =>
    sort.column === "canonical"
      ? item.canonical
      : sort.column === "code"
        ? item.code
        : sort.column === "valueKind"
          ? valueKindLabel(item.valueKind)
          : item.unit;
  return ordered(items, value, sort.direction);
}

export function filterUnits(units: Unit[], query: string): Unit[] {
  return units.filter((unit) =>
    contains(
      [
        unit.symbol,
        unit.code,
        unit.dimension,
        dimensionLabel(unit.dimension),
        ...unit.synonyms,
      ],
      query,
    ),
  );
}

export function sortUnits(units: Unit[], sort: Sort<UnitColumn> | null): Unit[] {
  if (!sort) return units;
  // Сортировка по размерности идёт по подписи: иначе видимый порядок
  // расходится с алфавитным — «Давление» стояло бы после «Мощности».
  const value = (unit: Unit) =>
    sort.column === "symbol"
      ? unit.symbol
      : sort.column === "code"
        ? unit.code
        : dimensionLabel(unit.dimension);
  return ordered(units, value, sort.direction);
}

/**
 * Отбор ветвей дерева материалов.
 *
 * Совпавший узел показывается вместе со всеми предками: иначе «AISI 316L»
 * всплыл бы без «Стали», и менеджер потерял бы место материала в иерархии.
 * Потомки совпавшего узла сохраняются целиком — ветвь остаётся ветвью.
 */
export function filterMaterials(nodes: MaterialNode[], query: string): MaterialNode[] {
  if (!normalize(query)) return nodes;
  const kept: MaterialNode[] = [];
  for (const node of nodes) {
    if (contains([node.name, node.code], query)) {
      kept.push(node);
      continue;
    }
    const children = filterMaterials(node.children, query);
    if (children.length) {
      kept.push({ ...node, children });
    }
  }
  return kept;
}

/** Переключение сортировки по колонке: asc → desc → без сортировки. */
export function toggleSort<TColumn extends string>(
  current: Sort<TColumn> | null,
  column: TColumn,
): Sort<TColumn> | null {
  if (current?.column !== column) return { column, direction: "asc" };
  if (current.direction === "asc") return { column, direction: "desc" };
  return null;
}
