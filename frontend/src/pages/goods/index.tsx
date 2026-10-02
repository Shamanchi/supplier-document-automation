import {
  ChevronLeft,
  ChevronRight,
  FileText,
  Search,
  Upload,
} from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { useAuth } from "@/features/auth/model";
import { useProject } from "@/features/project/model";
import { extractApiError } from "@/shared/api/client";
import { useProducts } from "@/shared/api/hooks";
import { mapCatalogItem } from "@/shared/api/mappers";
import {
  useBackendSuppliers,
  useImportedItems,
} from "@/shared/api/resource-hooks";
import { env } from "@/shared/config/env";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { ErrorState, LoadingState, PageHeader, Panel } from "@/shared/ui/page";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

const PAGE_SIZE = 50;

export function GoodsPage() {
  return env.enableMsw ? <MockGoodsPage /> : <LiveGoodsPage />;
}

function MockGoodsPage() {
  const { isAdminLike } = useAuth();
  const [category, setCategory] = useState("Извещатели");
  const [search, setSearch] = useState("");
  const { data, isLoading, isError } = useProducts({ category, search });

  return (
    <>
      <PageHeader
        title="Товары"
        subtitle="Поиск товаров по категориям и техническим характеристикам"
        actions={
          isAdminLike ? (
            <Button onClick={() => toast.success("Файл товара добавлен в очередь")}>
              <Upload />
              Загрузить документ
            </Button>
          ) : null
        }
      />
      <Panel>
        <div className="filter-row">
          <div className="filter-group">
            <label>Категория</label>
            <Select value={category} onValueChange={setCategory}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все категории</SelectItem>
                <SelectItem value="Огнетушители">Огнетушители</SelectItem>
                <SelectItem value="Извещатели">Извещатели</SelectItem>
                <SelectItem value="Шкафы">Шкафы</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="filter-group grow">
            <label htmlFor="goods-search">ТХ</label>
            <Input
              id="goods-search"
              placeholder="например: напряжение 12В"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <Button size="sm">
            <Search />
            Найти
          </Button>
        </div>
        {isLoading ? (
          <LoadingState />
        ) : isError || !data ? (
          <ErrorState />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Артикул</TableHead>
                <TableHead>Наименование</TableHead>
                <TableHead>Категория</TableHead>
                <TableHead>ТХ</TableHead>
                <TableHead>Ед.</TableHead>
                <TableHead>Поставщик</TableHead>
                <TableHead>Документ</TableHead>
                {isAdminLike ? <TableHead>Действия</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((product) => (
                <TableRow key={product.id}>
                  <TableCell className="font-semibold">{product.sku}</TableCell>
                  <TableCell>{product.name}</TableCell>
                  <TableCell>{product.category}</TableCell>
                  <TableCell>{product.specifications}</TableCell>
                  <TableCell>{product.unit}</TableCell>
                  <TableCell>{product.supplier}</TableCell>
                  <TableCell>
                    <Button
                      variant="link"
                      onClick={() =>
                        toast.info(`Открыт документ: ${product.documentType}`)
                      }
                    >
                      <FileText />
                      {product.documentType}
                    </Button>
                  </TableCell>
                  {isAdminLike ? (
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          toast.success(`Документ для ${product.sku} загружен`)
                        }
                      >
                        <Upload />
                        Загрузить
                      </Button>
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>
    </>
  );
}

function LiveGoodsPage() {
  const { project } = useProject();
  const [searchDraft, setSearchDraft] = useState("");
  const [supplierDraft, setSupplierDraft] = useState("all");
  const [query, setQuery] = useState("");
  const [supplierId, setSupplierId] = useState("");
  const [offset, setOffset] = useState(0);

  const suppliersQuery = useBackendSuppliers();
  const itemsQuery = useImportedItems(project.id, {
    query,
    supplierId,
    offset,
    limit: PAGE_SIZE,
  });
  const items = useMemo(
    () => itemsQuery.data?.items.map(mapCatalogItem) ?? [],
    [itemsQuery.data],
  );

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setOffset(0);
    setQuery(searchDraft.trim());
    setSupplierId(supplierDraft === "all" ? "" : supplierDraft);
  };

  const resetFilters = () => {
    setSearchDraft("");
    setSupplierDraft("all");
    setQuery("");
    setSupplierId("");
    setOffset(0);
  };

  return (
    <>
      <PageHeader
        title="Товары"
        subtitle="Позиции из разобранных прайсов выбранного проекта"
      />
      <Panel
        title="Каталог"
        actions={
          <span className="text-sm text-muted-foreground">
            {itemsQuery.data?.total.toLocaleString("ru-RU") ?? 0} позиций
          </span>
        }
      >
        <form className="filter-row" onSubmit={applyFilters}>
          <div className="filter-group min-w-64">
            <label htmlFor="goods-supplier">Поставщик</label>
            <Select value={supplierDraft} onValueChange={setSupplierDraft}>
              <SelectTrigger id="goods-supplier">
                <SelectValue placeholder="Все поставщики" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все поставщики</SelectItem>
                {suppliersQuery.data?.map((supplier) => (
                  <SelectItem key={supplier.id} value={supplier.id}>
                    {supplier.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="filter-group grow">
            <label htmlFor="goods-search">Название или артикул</label>
            <Input
              id="goods-search"
              placeholder="Введите часть названия или артикула"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
            />
          </div>
          <Button type="submit" size="sm">
            <Search />
            Найти
          </Button>
          {query || supplierId ? (
            <Button type="button" size="sm" variant="outline" onClick={resetFilters}>
              Сбросить
            </Button>
          ) : null}
        </form>

        {suppliersQuery.isError ? (
          <div className="border-b border-border px-5 py-3 text-sm text-destructive">
            Список поставщиков недоступен: {extractApiError(suppliersQuery.error)}
          </div>
        ) : null}

        {itemsQuery.isLoading ? (
          <LoadingState label="Загрузка товаров..." />
        ) : itemsQuery.isError ? (
          <ErrorState label={extractApiError(itemsQuery.error)} />
        ) : items.length ? (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Артикул</TableHead>
                  <TableHead>Наименование</TableHead>
                  <TableHead>Категория</TableHead>
                  <TableHead>Извлечённые параметры</TableHead>
                  <TableHead>Ед.</TableHead>
                  <TableHead>Поставщик</TableHead>
                  <TableHead>Источник</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell className="font-semibold">
                      {item.article ?? "—"}
                    </TableCell>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{item.category ?? "Не назначена"}</TableCell>
                    <TableCell className="max-w-96">
                      {item.attributes.length
                        ? item.attributes.slice(0, 4).join("; ")
                        : "—"}
                    </TableCell>
                    <TableCell>{item.unit ?? "—"}</TableCell>
                    <TableCell>{item.supplier}</TableCell>
                    <TableCell>
                      <div className="flex items-start gap-2">
                        <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                        <div>
                          <div>{item.filename}</div>
                          <div className="text-xs text-muted-foreground">
                            Версия {item.versionNo}
                          </div>
                        </div>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              offset={offset}
              total={itemsQuery.data?.total ?? 0}
              onChange={setOffset}
            />
          </>
        ) : (
          <div className="empty-state">Товары по заданным фильтрам не найдены</div>
        )}
      </Panel>
    </>
  );
}

function Pagination({
  offset,
  total,
  onChange,
}: {
  offset: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  if (total <= PAGE_SIZE) return null;
  const from = offset + 1;
  const to = Math.min(offset + PAGE_SIZE, total);
  return (
    <div className="flex items-center justify-between border-t border-border px-4 py-3">
      <span className="text-sm text-muted-foreground">
        {from}–{to} из {total.toLocaleString("ru-RU")}
      </span>
      <div className="flex gap-2">
        <Button
          type="button"
          size="icon"
          variant="outline"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}
          aria-label="Предыдущая страница"
        >
          <ChevronLeft />
        </Button>
        <Button
          type="button"
          size="icon"
          variant="outline"
          disabled={offset + PAGE_SIZE >= total}
          onClick={() => onChange(offset + PAGE_SIZE)}
          aria-label="Следующая страница"
        >
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}
