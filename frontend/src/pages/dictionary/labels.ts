/**
 * Подписи машинных значений каталога.
 *
 * В API остаются коды (`PRESSURE`, `NUMBER`): они не зависят от языка и по ним
 * работают фильтры и сравнение. Человеку показывается подпись — экран словаря
 * читают менеджеры, а не разработчики.
 *
 * Неизвестный код показывается как есть: `dimension` в каталоге — свободный
 * текст, закрытого перечня у него нет, и прятать незнакомое значение за
 * прочерком значило бы скрыть от менеджера содержимое справочника.
 */

/** Машинные значения `value_kind` каталога. */
export const VALUE_KIND_LABELS: Record<string, string> = {
  NUMBER: "Число",
  RANGE: "Диапазон",
  TEXT: "Текст",
  BOOLEAN: "Да/нет",
  SET: "Набор значений",
  MATERIAL: "Материал",
  UNKNOWN: "Не определён",
};

/** Физические размерности единиц и параметров. */
export const DIMENSION_LABELS: Record<string, string> = {
  PRESSURE: "Давление",
  TEMPERATURE: "Температура",
  LENGTH: "Длина",
  AREA: "Площадь",
  VOLUME: "Объём",
  MASS: "Масса",
  DENSITY: "Плотность",
  FORCE: "Сила",
  POWER: "Мощность",
  VOLTAGE: "Напряжение",
  CURRENT: "Сила тока",
  FREQUENCY: "Частота",
  TIME: "Время",
  SPEED: "Скорость",
  ROTATION_SPEED: "Частота вращения",
  VOLUMETRIC_FLOW: "Объёмный расход",
  MASS_FLOW: "Массовый расход",
  DIMENSIONLESS: "Безразмерная",
};

export function valueKindLabel(code: string): string {
  return VALUE_KIND_LABELS[code] ?? code;
}

export function dimensionLabel(code: string | null): string | null {
  if (!code) return null;
  return DIMENSION_LABELS[code] ?? code;
}
