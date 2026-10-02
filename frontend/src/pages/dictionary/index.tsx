import {
  ArrowDown,
  ArrowUp,
  BookOpen,
  Info,
  PenLine,
  Ruler,
  ListChecks,
  Search,
  Waypoints,
} from "lucide-react";
import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import {
  useDictionary,
  useMySuggestions,
  useSuggestSynonym,
} from "@/shared/api/hooks";
import { cn } from "@/shared/lib/utils";
import type { MaterialNode } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/dialog";
import { Input } from "@/shared/ui/input";
import { Label } from "@/shared/ui/label";
import {
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  StatusBadge,
  type StatusTone,
} from "@/shared/ui/page";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import {
  ANY,
  EMPTY_NOMENCLATURE_FILTERS,
  filterMaterials,
  filterNomenclature,
  filterUnits,
  optionsOf,
  type NomenclatureColumn,
  type NomenclatureFilters,
  type Sort,
  type UnitColumn,
  sortNomenclature,
  sortUnits,
  toggleSort,
} from "@/pages/dictionary/filtering";
import { dimensionLabel, valueKindLabel } from "@/pages/dictionary/labels";

const synonymSchema = z.object({
  synonym: z.string().min(2, "Введите синоним"),
});

const DASH = "—";

/** Заголовок-кнопка: сортировка переключается asc → desc → без сортировки. */
function SortableHead<TColumn extends string>({
  column,
  sort,
  onSort,
  children,
}: {
  column: TColumn;
  sort: Sort<TColumn> | null;
  onSort: (column: TColumn) => void;
  children: React.ReactNode;
}) {
  const active = sort?.column === column ? sort.direction : null;
  return (
    <TableHead>
      <button className="sort-head" onClick={() => onSort(column)} type="button">
        {children}
        {active === "asc" ? <ArrowUp className="size-3.5" /> : null}
        {active === "desc" ? <ArrowDown className="size-3.5" /> : null}
      </button>
    </TableHead>
  );
}

function SearchBox({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <div className="dict-search">
      <Search className="size-4 text-muted-foreground" />
      <Input
        aria-label={placeholder}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        value={value}
      />
    </div>
  );
}

/** Ничего не нашлось — это не пустой каталог, и сообщение должно отличаться. */
function NothingFound() {
  return (
    <div className="p-5 text-sm text-muted-foreground">
      <Info className="mr-2 inline size-4" />
      Ничего не найдено. Измените условия поиска или сбросьте фильтры.
    </div>
  );
}

function MaterialBranch({ node }: { node: MaterialNode }) {
  return (
    <div className={cn("mt-node", node.children.length === 0 && "leaf")}>
      {node.name}
      {node.children.map((child) => (
        <MaterialBranch key={child.id} node={child} />
      ))}
    </div>
  );
}

/** Статус предложения синонима — в подпись и тон бейджа. */
const SUGGESTION_STATUS: Record<string, { label: string; tone: StatusTone }> = {
  pending: { label: "Ожидает решения", tone: "info" },
  accepted: { label: "Принят", tone: "success" },
  rejected: { label: "Отклонён", tone: "danger" },
};

function formatMoment(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

/**
 * Свои предложения синонимов.
 *
 * Без этого раздела судьба предложения была видна только в toast-сообщении:
 * менеджер отправлял синоним и больше о нём ничего не узнавал.
 */
function MySuggestions() {
  const { data, isLoading, isError } = useMySuggestions();

  if (isLoading) return <LoadingState label="Загрузка предложений..." />;
  if (isError || !data) return <ErrorState label="Не удалось загрузить предложения" />;
  if (data.length === 0) {
    return (
      <EmptySection label="Вы ещё не предлагали синонимов. Кнопка «Предложить» — во вкладке «Номенклатура»." />
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Синоним</TableHead>
          <TableHead>Канонический параметр</TableHead>
          <TableHead>Статус</TableHead>
          <TableHead>Предложен</TableHead>
          <TableHead>Решение</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((suggestion) => {
          const status =
            SUGGESTION_STATUS[suggestion.status] ?? {
              label: suggestion.status,
              tone: "archived" as StatusTone,
            };
          return (
            <TableRow key={suggestion.id}>
              <TableCell className="font-semibold">{suggestion.synonym}</TableCell>
              <TableCell>{suggestion.canonical}</TableCell>
              <TableCell>
                <StatusBadge label={status.label} tone={status.tone} />
              </TableCell>
              <TableCell className="text-muted-foreground">
                {formatMoment(suggestion.createdAt)}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {formatMoment(suggestion.decidedAt)}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

/** Пустая вкладка: каталог опубликован, но раздел в нём не заполнен. */
function EmptySection({ label }: { label: string }) {
  return (
    <div className="p-5 text-sm text-muted-foreground">
      <Info className="mr-2 inline size-4" />
      {label}
    </div>
  );
}

export function DictionaryPage() {
  const { data, isLoading, isError } = useDictionary();
  const suggest = useSuggestSynonym();
  // Хранится сам параметр, а не его имя: в запрос уходит идентификатор.
  const [target, setTarget] = useState<{ id: string; canonical: string } | null>(
    null,
  );
  const [filters, setFilters] = useState<NomenclatureFilters>(
    EMPTY_NOMENCLATURE_FILTERS,
  );
  const [nomenclatureSort, setNomenclatureSort] =
    useState<Sort<NomenclatureColumn> | null>(null);
  const [unitQuery, setUnitQuery] = useState("");
  const [unitSort, setUnitSort] = useState<Sort<UnitColumn> | null>(null);
  const [materialQuery, setMaterialQuery] = useState("");
  const form = useForm<z.infer<typeof synonymSchema>>({
    resolver: zodResolver(synonymSchema),
    defaultValues: { synonym: "" },
  });

  const close = () => {
    // Сброс формы на закрытии: иначе введённый текст всплывает при открытии
    // диалога для другого параметра.
    form.reset();
    setTarget(null);
  };

  const submit = form.handleSubmit(async ({ synonym }) => {
    if (!target) return;
    try {
      await suggest.mutateAsync({
        canonical: target.canonical,
        canonicalParameterId: target.id,
        synonym,
      });
      toast.success("Синоним отправлен Администратору на подтверждение");
      close();
    } catch (cause) {
      // Каталог отвергает дубликаты и синонимы неизвестного параметра —
      // сообщение приходит из envelope'а, показывать его нужно как есть.
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось отправить синоним",
      );
    }
  });

  if (isLoading) return <LoadingState />;
  if (isError || !data) return <ErrorState />;

  // Без опубликованной версии каталога словарю нечего показывать: черновики в
  // проекцию не попадают, это не ошибка загрузки.
  if (data.catalogVersion === null) {
    return (
      <>
        <PageHeader
          title="Словарь"
          subtitle="Номенклатура, единицы измерения и материалы — справочник для всех ролей"
        />
        <Panel title="Каталог не опубликован">
          <EmptySection label="Ни одна версия каталога ещё не опубликована. Словарь появится после публикации версии в разделе «Управление»." />
        </Panel>
      </>
    );
  }

  // Считается после ранних return: списки словаря невелики, и мемоизация здесь
  // была бы преждевременной, а хуки после условного возврата объявлять нельзя.
  const nomenclature = sortNomenclature(
    filterNomenclature(data.nomenclature, filters),
    nomenclatureSort,
  );
  const units = sortUnits(filterUnits(data.units, unitQuery), unitSort);
  const materials = filterMaterials(data.materials, materialQuery);
  const valueKinds = optionsOf(data.nomenclature, (item) => item.valueKind);
  const dimensions = optionsOf(data.nomenclature, (item) => item.dimension);

  return (
    <>
      <PageHeader
        title="Словарь"
        subtitle={`Опубликованная версия каталога: ${data.catalogVersion}. Редактирование — в разделе «Управление»`}
      />
      <Tabs defaultValue="nomenclature">
        <TabsList>
          <TabsTrigger value="nomenclature">
            <BookOpen />
            Номенклатура
          </TabsTrigger>
          <TabsTrigger value="units">
            <Ruler />
            Единицы измерения
          </TabsTrigger>
          <TabsTrigger value="materials">
            <Waypoints />
            Материалы
          </TabsTrigger>
          <TabsTrigger value="suggestions">
            <ListChecks />
            Мои предложения
          </TabsTrigger>
        </TabsList>
        <TabsContent value="nomenclature">
          <Panel
            title="Канонические параметры и синонимы"
            actions={
              <span className="dict-readonly-hint">
                <Info className="size-4" />
                Менеджер предлагает синонимы, Администратор подтверждает
              </span>
            }
          >
            {data.nomenclature.length === 0 ? (
              <EmptySection label="В опубликованной версии каталога нет параметров." />
            ) : (
              <>
                <div className="filter-row">
                  <SearchBox
                    onChange={(query) => setFilters({ ...filters, query })}
                    placeholder="Поиск по названию, коду и синонимам"
                    value={filters.query}
                  />
                  <div className="filter-group">
                    <label>Тип значения</label>
                    <Select
                      onValueChange={(valueKind) =>
                        setFilters({ ...filters, valueKind })
                      }
                      value={filters.valueKind}
                    >
                      <SelectTrigger aria-label="Тип значения">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={ANY}>Все</SelectItem>
                        {valueKinds.map((kind) => (
                          <SelectItem key={kind} value={kind}>
                            {valueKindLabel(kind)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="filter-group">
                    <label>Размерность</label>
                    <Select
                      onValueChange={(dimension) =>
                        setFilters({ ...filters, dimension })
                      }
                      value={filters.dimension}
                    >
                      <SelectTrigger aria-label="Размерность">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={ANY}>Все</SelectItem>
                        {dimensions.map((dimension) => (
                          <SelectItem key={dimension} value={dimension}>
                            {dimensionLabel(dimension)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="filter-group">
                    <label>Единица</label>
                    <Select
                      onValueChange={(unit) => setFilters({ ...filters, unit })}
                      value={filters.unit}
                    >
                      <SelectTrigger aria-label="Наличие единицы">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={ANY}>Все</SelectItem>
                        <SelectItem value="with">С единицей</SelectItem>
                        <SelectItem value="without">Без единицы</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <Button
                    onClick={() => setFilters(EMPTY_NOMENCLATURE_FILTERS)}
                    size="sm"
                    variant="outline"
                  >
                    Сбросить
                  </Button>
                  <span className="text-sm text-muted-foreground">
                    Показано {nomenclature.length} из {data.nomenclature.length}
                  </span>
                </div>
                {nomenclature.length === 0 ? (
                  <NothingFound />
                ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableHead
                      column="canonical"
                      onSort={(column) =>
                        setNomenclatureSort(toggleSort(nomenclatureSort, column))
                      }
                      sort={nomenclatureSort}
                    >
                      Каноническое название
                    </SortableHead>
                    <SortableHead
                      column="code"
                      onSort={(column) =>
                        setNomenclatureSort(toggleSort(nomenclatureSort, column))
                      }
                      sort={nomenclatureSort}
                    >
                      Код
                    </SortableHead>
                    <SortableHead
                      column="valueKind"
                      onSort={(column) =>
                        setNomenclatureSort(toggleSort(nomenclatureSort, column))
                      }
                      sort={nomenclatureSort}
                    >
                      Тип значения
                    </SortableHead>
                    <SortableHead
                      column="unit"
                      onSort={(column) =>
                        setNomenclatureSort(toggleSort(nomenclatureSort, column))
                      }
                      sort={nomenclatureSort}
                    >
                      Единица
                    </SortableHead>
                    <TableHead>Синонимы</TableHead>
                    <TableHead>Действия</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {nomenclature.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="font-semibold">
                        {item.canonical}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {item.code}
                      </TableCell>
                      <TableCell>
                        {valueKindLabel(item.valueKind)}
                      </TableCell>
                      <TableCell>{item.unit ?? DASH}</TableCell>
                      <TableCell>
                        {item.synonyms.length
                          ? item.synonyms.join(", ")
                          : DASH}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            setTarget({ id: item.id, canonical: item.canonical })
                          }
                        >
                          <PenLine />
                          Предложить
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
                )}
              </>
            )}
          </Panel>
        </TabsContent>
        <TabsContent value="units">
          <Panel
            title="Единицы измерения (конвертер)"
            actions={
              <span className="dict-readonly-hint">
                <Info className="size-4" />
                Только просмотр
              </span>
            }
          >
            {data.units.length === 0 ? (
              <EmptySection label="В опубликованной версии каталога нет единиц измерения." />
            ) : (
              <>
                <div className="filter-row">
                  <SearchBox
                    onChange={setUnitQuery}
                    placeholder="Поиск по обозначению, коду и размерности"
                    value={unitQuery}
                  />
                  <span className="text-sm text-muted-foreground">
                    Показано {units.length} из {data.units.length}
                  </span>
                </div>
                {units.length === 0 ? (
                  <NothingFound />
                ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableHead
                      column="symbol"
                      onSort={(column) => setUnitSort(toggleSort(unitSort, column))}
                      sort={unitSort}
                    >
                      Единица
                    </SortableHead>
                    <SortableHead
                      column="code"
                      onSort={(column) => setUnitSort(toggleSort(unitSort, column))}
                      sort={unitSort}
                    >
                      Код
                    </SortableHead>
                    <SortableHead
                      column="dimension"
                      onSort={(column) => setUnitSort(toggleSort(unitSort, column))}
                      sort={unitSort}
                    >
                      Размерность
                    </SortableHead>
                    <TableHead>Перевод</TableHead>
                    <TableHead>Базовая единица</TableHead>
                    <TableHead>Синонимы</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {units.map((unit) => (
                    <TableRow key={unit.id}>
                      <TableCell className="font-semibold">
                        {unit.symbol}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {unit.code}
                      </TableCell>
                      <TableCell>{dimensionLabel(unit.dimension)}</TableCell>
                      <TableCell className="tabular-nums">
                        {unit.conversion ?? "базовая"}
                      </TableCell>
                      <TableCell>{unit.baseUnit ?? DASH}</TableCell>
                      <TableCell>
                        {unit.synonyms.length ? unit.synonyms.join(", ") : DASH}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
                )}
              </>
            )}
          </Panel>
        </TabsContent>
        <TabsContent value="materials">
          <Panel
            title="Иерархия материалов"
            actions={
              <span className="dict-readonly-hint">
                <Info className="size-4" />
                Только просмотр
              </span>
            }
          >
            {data.materials.length === 0 ? (
              <EmptySection label="В опубликованной версии каталога нет материалов." />
            ) : (
              <>
                <div className="filter-row">
                  <SearchBox
                    onChange={setMaterialQuery}
                    placeholder="Поиск по названию и коду материала"
                    value={materialQuery}
                  />
                </div>
                {materials.length === 0 ? (
                  <NothingFound />
                ) : (
              <div className="p-5">
                <div className="material-tree">
                  {materials.map((material) => (
                    <MaterialBranch key={material.id} node={material} />
                  ))}
                </div>
                <div className="rule-hint mt-3">
                  <Info className="size-4" />
                  Иерархия используется при сравнении ТХ: AISI 316L
                  засчитывается как соответствие требованию «Сталь».
                </div>
              </div>
                )}
              </>
            )}
          </Panel>
        </TabsContent>
        <TabsContent value="suggestions">
          <Panel
            title="Мои предложения синонимов"
            actions={
              <span className="dict-readonly-hint">
                <Info className="size-4" />
                Принятый синоним попадёт в словарь после публикации каталога
              </span>
            }
          >
            <MySuggestions />
          </Panel>
        </TabsContent>
      </Tabs>

      <Dialog open={Boolean(target)} onOpenChange={close}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <PenLine />
              Предложить синоним
            </DialogTitle>
            <DialogDescription>
              Каноническое название: {target?.canonical}
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={submit}>
            <div className="grid gap-2">
              <Label htmlFor="synonym">Новый синоним</Label>
              <Input id="synonym" {...form.register("synonym")} />
              <span className="field-error">
                {form.formState.errors.synonym?.message}
              </span>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={close}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={suggest.isPending}>
                Отправить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
