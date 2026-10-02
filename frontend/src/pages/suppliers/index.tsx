import {
  AlertTriangle,
  BadgePercent,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FileBadge,
  FileText,
  Info,
  Loader2,
  Plus,
  RefreshCw,
  Upload,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { useAuth } from "@/features/auth/model";
import { useProject } from "@/features/project/model";
import { extractApiError } from "@/shared/api/client";
import {
  useCreateSupplier,
  useSuppliers,
  useUploadSupplierFile,
} from "@/shared/api/hooks";
import {
  formatImportPriceDetails,
  importPriceKey,
  importPriceLabel,
  mapImportIssue,
  mapImportItem,
  mapImportStatus,
} from "@/shared/api/mappers";
import {
  enabledLlmProfiles,
  selectEnabledLlmProfile,
} from "@/shared/api/llm-profiles";
import {
  useCreatePriceImport,
  useLlmProfiles,
  usePriceImport,
  usePriceImportIssues,
  usePriceImportItems,
  usePriceImports,
  useUpdatePriceImport,
} from "@/shared/api/resource-hooks";
import { env } from "@/shared/config/env";
import { formatMoney } from "@/shared/lib/utils";
import type { OrderFile } from "@/shared/model/types";
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
import { ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from "@/shared/ui/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

const supplierSchema = z.object({
  name: z.string().min(3, "Укажите название"),
  email: z.string().email("Некорректный e-mail"),
});

const fileSchema = z.object({
  fileName: z.string().min(3, "Укажите имя файла"),
  kind: z.enum(["price", "passport", "other"]),
});

export function SuppliersPage() {
  return env.enableMsw ? <MockSuppliersPage /> : <LiveSuppliersPage />;
}

function MockSuppliersPage() {
  const { isAdminLike } = useAuth();
  const { data, isLoading, isError } = useSuppliers();
  const createSupplier = useCreateSupplier();
  const uploadFile = useUploadSupplierFile();
  const [selectedId, setSelectedId] = useState("s-1");
  const [addOpen, setAddOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [priceFile, setPriceFile] = useState<OrderFile | null>(null);
  const supplierForm = useForm<z.infer<typeof supplierSchema>>({
    resolver: zodResolver(supplierSchema),
    defaultValues: { name: "", email: "" },
  });
  const fileForm = useForm<z.infer<typeof fileSchema>>({
    resolver: zodResolver(fileSchema),
    defaultValues: { fileName: "", kind: "price" },
  });
  const selected = useMemo(
    () => data?.find((item) => item.id === selectedId) ?? data?.[0],
    [data, selectedId],
  );

  if (isLoading) return <LoadingState />;
  if (isError || !data || !selected) return <ErrorState />;

  const submitSupplier = supplierForm.handleSubmit(async (values) => {
    const supplier = await createSupplier.mutateAsync(values);
    setSelectedId(supplier.id);
    setAddOpen(false);
    supplierForm.reset();
    toast.success("Поставщик добавлен");
  });

  const submitFile = fileForm.handleSubmit(async (values) => {
    await uploadFile.mutateAsync({
      supplierId: selected.id,
      ...values,
    });
    setUploadOpen(false);
    fileForm.reset();
    toast.success("Файл поставщика загружен");
  });

  return (
    <>
      <PageHeader
        title="Поставщики"
        subtitle="Справочник поставщиков, товары, цены и документация"
        actions={
          isAdminLike ? (
            <>
              <Button onClick={() => setAddOpen(true)}>
                <Plus />
                Добавить поставщика
              </Button>
              <Button onClick={() => setUploadOpen(true)}>
                <Upload />
                Загрузить прайс
              </Button>
            </>
          ) : null
        }
      />
      <Panel
        title="Выберите поставщика"
        actions={
          <span className="text-sm text-muted-foreground">
            {data.length} поставщика
          </span>
        }
      >
        <div className="flex flex-wrap gap-3 border-b border-border px-5 py-3">
          {data.map((supplier) => (
            <Button
              key={supplier.id}
              variant={supplier.id === selected.id ? "orange" : "default"}
              size="sm"
              onClick={() => setSelectedId(supplier.id)}
            >
              {supplier.name}
            </Button>
          ))}
        </div>
        <div className="p-5">
          <div className="mb-2 font-semibold">Товары поставщика</div>
          <div className="mb-4 flex flex-wrap gap-2">
            {selected.products.map((product, index) => (
              <span
                className={index === 0 ? "source-tag manager" : "source-tag bitrix"}
                key={product}
              >
                {product}
              </span>
            ))}
          </div>
          <div className="mb-2 flex items-center gap-2 font-semibold">
            <FileText className="size-4" />
            Файлы поставщика
          </div>
          <div className="data-list">
            {selected.files.length ? (
              selected.files.map((file) => (
                <div className="file-item" key={file.id}>
                  <FileText className="file-icon" />
                  <span className="file-name">{file.name}</span>
                  <span className="file-version">{file.version}</span>
                  <span className="file-date">{file.date}</span>
                  <span className="file-status text-green-600">
                    <CheckCircle2 className="size-4" />
                    Обработан
                  </span>
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() =>
                      file.kind === "price"
                        ? setPriceFile(file)
                        : toast.info(`Открыт документ ${file.name}`)
                    }
                    aria-label={`Открыть ${file.name}`}
                  >
                    <FileText />
                  </Button>
                </div>
              ))
            ) : (
              <div className="empty-state">Файлы ещё не загружены</div>
            )}
          </div>
          {isAdminLike ? (
            <Button
              className="mt-3"
              variant="outline"
              size="sm"
              onClick={() => setUploadOpen(true)}
            >
              <Upload />
              Загрузить файл
            </Button>
          ) : null}
        </div>

        <Tabs defaultValue="prices">
          <TabsList className="border-x-0 border-b-0 border-t">
            <TabsTrigger value="prices">
              <BadgePercent />
              Цены и скидки
            </TabsTrigger>
            <TabsTrigger value="certificates">
              <FileBadge />
              Документы
            </TabsTrigger>
          </TabsList>
          <TabsContent value="prices" className="px-5 pb-5">
            <div className="mb-3 text-sm text-muted-foreground">
              Показатели по товару:{" "}
              <strong className="text-foreground">
                {selected.pricing.product}
              </strong>
            </div>
            <div className="supplier-price-grid">
              <div className="price-cell">
                <div className="price-label">Цена закупки</div>
                <div className="price-value">
                  {formatMoney(selected.pricing.purchasePrice)}
                </div>
              </div>
              <div className="price-cell">
                <div className="price-label">Цена розница</div>
                <div className="price-value">
                  {formatMoney(selected.pricing.retailPrice)}
                </div>
              </div>
              <div className="price-cell">
                <div className="price-label">Скидка (категория)</div>
                <div className="price-value text-green-600">
                  −{selected.pricing.categoryDiscount}%
                </div>
              </div>
              <div className="price-cell">
                <div className="price-label">Наценка</div>
                <div className="price-value text-[#F26419]">
                  +{selected.pricing.markup}%
                </div>
              </div>
            </div>
            <div className="supplier-dates">
              <div>
                <span className="text-muted-foreground">
                  Дата загрузки прайса:
                </span>{" "}
                <strong>{selected.pricing.priceUploadedAt}</strong>
              </div>
              <div>
                <span className="text-muted-foreground">
                  Дата обновления документа:
                </span>{" "}
                <strong>{selected.pricing.certificateUpdatedAt}</strong>
              </div>
            </div>
            <div className="mb-2 mt-4 font-semibold">
              Правила скидок поставщика
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Тип скидки</TableHead>
                  <TableHead>Процент</TableHead>
                  <TableHead>Условие</TableHead>
                  <TableHead>Статус</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.discountRules.map((rule) => (
                  <TableRow key={rule.id}>
                    <TableCell>{rule.type}</TableCell>
                    <TableCell className="font-semibold tabular-nums">
                      {rule.percent}%
                    </TableCell>
                    <TableCell>{rule.condition}</TableCell>
                    <TableCell>
                      <StatusBadge
                        label={rule.active ? "Активна" : "Отключена"}
                        tone={rule.active ? "success" : "archived"}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="rule-hint">
              <Info className="size-4" />
              Скидки не суммируются: применяется максимальная подходящая.
            </div>
          </TabsContent>
          <TabsContent value="certificates" className="px-5 pb-5">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Документ</TableHead>
                  <TableHead>Тип</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Действует до</TableHead>
                  <TableHead>Источник</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.certificates.map((certificate) => (
                  <TableRow key={certificate.id}>
                    <TableCell className="font-semibold">
                      {certificate.number}
                    </TableCell>
                    <TableCell>{certificate.type}</TableCell>
                    <TableCell>
                      <StatusBadge
                        label={
                          certificate.status === "active"
                            ? "Активен"
                            : "Истёк"
                        }
                        tone={
                          certificate.status === "active"
                            ? "success"
                            : "danger"
                        }
                      />
                    </TableCell>
                    <TableCell>{certificate.validUntil}</TableCell>
                    <TableCell>
                      <Button
                        variant="link"
                        onClick={() => toast.info("Документ открыт")}
                      >
                        <FileText />
                        Открыть
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TabsContent>
        </Tabs>
      </Panel>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <Plus />
              Новый поставщик
            </DialogTitle>
            <DialogDescription>
              Добавьте карточку поставщика в общий справочник.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={submitSupplier}>
            <div className="grid gap-2">
              <Label htmlFor="supplier-name">Название</Label>
              <Input id="supplier-name" {...supplierForm.register("name")} />
              <span className="field-error">
                {supplierForm.formState.errors.name?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="supplier-email">E-mail</Label>
              <Input
                id="supplier-email"
                type="email"
                {...supplierForm.register("email")}
              />
              <span className="field-error">
                {supplierForm.formState.errors.email?.message}
              </span>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setAddOpen(false)}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={createSupplier.isPending}>
                Добавить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <Upload />
              Загрузка файла
            </DialogTitle>
            <DialogDescription>{selected.name}</DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={submitFile}>
            <div className="grid gap-2">
              <Label htmlFor="supplier-file">Имя файла</Label>
              <Input id="supplier-file" {...fileForm.register("fileName")} />
              <span className="field-error">
                {fileForm.formState.errors.fileName?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="supplier-file-kind">Тип документа</Label>
              <select
                id="supplier-file-kind"
                className="filter-select"
                {...fileForm.register("kind")}
              >
                <option value="price">Прайс</option>
                <option value="passport">Паспорт</option>
                <option value="other">Прочее</option>
              </select>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setUploadOpen(false)}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={uploadFile.isPending}>
                <Upload />
                Загрузить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(priceFile)} onOpenChange={() => setPriceFile(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <FileText />
              Версия прайса
            </DialogTitle>
          </DialogHeader>
          {priceFile ? (
            <div className="data-list">
              <div className="data-list-row">
                <span className="text-muted-foreground">Файл</span>
                <strong>{priceFile.name}</strong>
              </div>
              <div className="data-list-row">
                <span className="text-muted-foreground">Версия</span>
                <span>{priceFile.version}</span>
              </div>
              <div className="data-list-row">
                <span className="text-muted-foreground">Дата</span>
                <span>{priceFile.date}</span>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => toast.success("Прайс скачан")}
            >
              <Upload className="rotate-180" />
              Скачать
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const LIVE_PAGE_SIZE = 50;

function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") {
    return `ui-${crypto.randomUUID()}`;
  }
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function LiveSuppliersPage() {
  const { isAdminLike, role } = useAuth();
  const { project } = useProject();
  const importsQuery = usePriceImports(project.id);
  const createImport = useCreatePriceImport(project.id);
  const updateImport = useUpdatePriceImport(project.id);
  const llmProfilesQuery = useLlmProfiles();
  const [selectedImportId, setSelectedImportId] = useState("");
  const [itemsOffset, setItemsOffset] = useState(0);
  const [issuesOffset, setIssuesOffset] = useState(0);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [supplierExternalId, setSupplierExternalId] = useState("");
  const [supplierName, setSupplierName] = useState("");
  const [currencyHint, setCurrencyHint] = useState("");
  const [pendingVatRate, setPendingVatRate] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey);
  const availableLlmProfiles = useMemo(
    () => enabledLlmProfiles(llmProfilesQuery.data),
    [llmProfilesQuery.data],
  );
  const selectedLlmProfile = useMemo(
    () => selectEnabledLlmProfile(llmProfilesQuery.data, provider),
    [llmProfilesQuery.data, provider],
  );

  useEffect(() => {
    if (!selectedImportId && importsQuery.data?.items[0]) {
      setSelectedImportId(importsQuery.data.items[0].id);
    }
  }, [importsQuery.data, selectedImportId]);

  useEffect(() => {
    setItemsOffset(0);
    setIssuesOffset(0);
  }, [selectedImportId]);

  useEffect(() => {
    if (!selectedLlmProfile) return;
    if (provider !== selectedLlmProfile.provider) {
      setProvider(selectedLlmProfile.provider);
    }
    if (!selectedLlmProfile.models.includes(model)) {
      setModel(selectedLlmProfile.default_model);
    }
  }, [model, provider, selectedLlmProfile]);

  const statusQuery = usePriceImport(project.id, selectedImportId);
  const status = statusQuery.data;
  const statusView = status ? mapImportStatus(status.status) : null;
  const terminal = statusView?.terminal ?? false;
  const itemsQuery = usePriceImportItems(
    project.id,
    selectedImportId,
    itemsOffset,
    LIVE_PAGE_SIZE,
    terminal,
  );
  const issuesQuery = usePriceImportIssues(
    project.id,
    selectedImportId,
    issuesOffset,
    LIVE_PAGE_SIZE,
    terminal,
  );

  const items = useMemo(
    () => itemsQuery.data?.items.map(mapImportItem) ?? [],
    [itemsQuery.data],
  );

  const [selectedPrices, setSelectedPrices] = useState<Record<string, string>>({});

  const issues = useMemo(
    () => issuesQuery.data?.issues.map(mapImportIssue) ?? [],
    [issuesQuery.data],
  );

  const resetUpload = () => {
    setFile(null);
    setSupplierExternalId("");
    setSupplierName("");
    setCurrencyHint("");
    setIdempotencyKey(newIdempotencyKey());
  };

  const closeUpload = () => {
    setUploadOpen(false);
    resetUpload();
  };

  const submitImport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file) {
      toast.error("Выберите файл прайса");
      return;
    }
    const externalId = supplierExternalId.trim();
    if (!externalId) {
      toast.error("Укажите код поставщика");
      return;
    }

    try {
      const accepted = await createImport.mutateAsync({
        file,
        supplierExternalId: externalId,
        supplierName: supplierName.trim() || undefined,
        currencyHint: currencyHint || undefined,
        vatRatePercent: undefined,
        ...(selectedLlmProfile
          ? {
              provider: selectedLlmProfile.provider,
              model: selectedLlmProfile.models.includes(model)
                ? model
                : selectedLlmProfile.default_model,
            }
          : {}),
        idempotencyKey,
      });
      setSelectedImportId(accepted.import_id);
      closeUpload();
      toast.success(
        accepted.replayed
          ? "Открыт ранее созданный импорт"
          : "Прайс принят в обработку",
      );
    } catch (error) {
      toast.error(extractApiError(error));
    }
  };

  return (
    <>
      <PageHeader
        title="Поставщики"
        subtitle="Загрузка прайс-листов и результаты серверного разбора"
        actions={
          isAdminLike ? (
            <Button type="button" onClick={() => setUploadOpen(true)}>
              <Upload />
              Загрузить прайс
            </Button>
          ) : null
        }
      />

      <Panel
        className="mb-6"
        title="История обработки"
        actions={
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <span>
              {importsQuery.data?.total.toLocaleString("ru-RU") ?? 0} заданий
            </span>
            <Button
              type="button"
              size="icon"
              variant="outline"
              onClick={() => void importsQuery.refetch()}
              aria-label="Обновить историю"
            >
              <RefreshCw className={importsQuery.isFetching ? "animate-spin" : ""} />
            </Button>
          </div>
        }
      >
        {importsQuery.isLoading ? (
          <LoadingState label="Загрузка истории..." />
        ) : importsQuery.isError ? (
          <ErrorState label={extractApiError(importsQuery.error)} />
        ) : importsQuery.data?.items.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Поставщик</TableHead>
                <TableHead>Файл</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Прогресс</TableHead>
                <TableHead>Позиций</TableHead>
                <TableHead>Создан</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {importsQuery.data.items.map((row) => {
                const view = mapImportStatus(row.status);
                return (
                  <TableRow
                    key={row.id}
                    data-state={row.id === selectedImportId ? "selected" : undefined}
                    className="cursor-pointer"
                    onClick={() => setSelectedImportId(row.id)}
                  >
                    <TableCell>
                      <div className="font-semibold">{row.supplier_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {row.supplier_external_id}
                      </div>
                    </TableCell>
                    <TableCell>{row.filename}</TableCell>
                    <TableCell>
                      <StatusBadge label={view.label} tone={view.tone} />
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {row.progress_percent}%
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {row.item_count.toLocaleString("ru-RU")}
                    </TableCell>
                    <TableCell>{formatDateTime(row.created_at)}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <div className="empty-state">
            Прайсы ещё не загружены
          </div>
        )}
      </Panel>

      <Panel
        title={
          status ? `${status.supplier_name}: ${status.filename}` : "Результат обработки"
        }
        actions={
          statusView ? (
            <StatusBadge label={statusView.label} tone={statusView.tone} />
          ) : null
        }
      >
        {!selectedImportId ? (
          <div className="empty-state">
            Выберите задание или загрузите новый прайс
          </div>
        ) : statusQuery.isLoading ? (
          <LoadingState label="Получение статуса..." />
        ) : statusQuery.isError || !status ? (
          <ErrorState
            label={
              statusQuery.isError
                ? extractApiError(statusQuery.error)
                : "Статус импорта недоступен"
            }
          />
        ) : (
          <>
            <div className="grid gap-4 border-b border-border p-5 md:grid-cols-4">
              <div>
                <div className="text-xs text-muted-foreground">Этап</div>
                <div className="mt-1 font-semibold">
                  {mapImportStatus(status.stage).label}
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Позиций</div>
                <div className="mt-1 font-semibold tabular-nums">
                  {status.item_count.toLocaleString("ru-RU")}
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Предупреждений</div>
                <div className="mt-1 font-semibold tabular-nums">
                  {status.warnings_count.toLocaleString("ru-RU")}
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Ошибок</div>
                <div className="mt-1 font-semibold tabular-nums">
                  {status.errors_count.toLocaleString("ru-RU")}
                </div>
              </div>
              <div className="md:col-span-4">
                <div className="mb-2 flex justify-between text-xs text-muted-foreground">
                  <span>Прогресс</span>
                  <span>{status.progress_percent}%</span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-muted">
                  <div
                    className="h-full bg-primary transition-[width]"
                    style={{ width: `${status.progress_percent}%` }}
                  />
                </div>
              </div>
              {status.error_message ? (
                <div className="md:col-span-4 flex items-start gap-2 text-sm text-destructive">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                  <span>{status.error_message}</span>
                </div>
              ) : null}
              {status.error_code === "VAT_RATE_REQUIRED" ? (
                <div className="md:col-span-4 flex flex-col gap-3 rounded-md border border-primary/30 bg-primary/5 p-4">
                  <div className="flex items-start gap-2">
                    <Info className="mt-0.5 size-4 shrink-0 text-primary" />
                    <div className="flex-1">
                      <p className="text-sm font-medium">Укажите входной НДС</p>
                      <p className="mt-1 text-sm text-muted-foreground">
                        Для завершения импорта необходимо указать ставку НДС.
                      </p>
                    </div>
                  </div>
                  <div className="flex items-end gap-3">
                    <div className="flex-1 grid gap-2">
                      <Label htmlFor="pending-vat-rate">Ставка НДС</Label>
                      <select
                        id="pending-vat-rate"
                        className="filter-select"
                        value={pendingVatRate}
                        onChange={(event) => setPendingVatRate(event.target.value)}
                      >
                        <option value="">— Выберите ставку —</option>
                        <option value="22">22%</option>
                        <option value="10">10%</option>
                        <option value="0">0% (без НДС)</option>
                      </select>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      disabled={!pendingVatRate || updateImport.isPending}
                      onClick={async () => {
                        try {
                          await updateImport.mutateAsync({
                            importId: status.id,
                            input: { vatRatePercent: pendingVatRate },
                          });
                          setPendingVatRate("");
                          toast.success("НДС указан, импорт продолжается");
                        } catch (error) {
                          toast.error(extractApiError(error));
                        }
                      }}
                    >
                      {updateImport.isPending ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <CheckCircle2 />
                      )}
                      Продолжить
                    </Button>
                  </div>
                </div>
              ) : null}
              {status.status === "review_required" ||
                status.error_code === "needs_mapping" ||
                status.error_code === "mapping_required" ? (
                role === "sysadmin" ? (
                  <div className="md:col-span-4 flex flex-wrap items-center gap-3 rounded-md border border-primary/30 bg-primary/5 p-3">
                    <span className="text-sm">Для этого импорта требуется настройка правила.</span>
                    <Button asChild size="sm">
                      <a href={`/engineering/imports/${encodeURIComponent(status.id)}`}>
                        <Wrench />
                        Настроить правило
                      </a>
                    </Button>
                  </div>
                ) : (
                  <div className="md:col-span-4 text-sm text-muted-foreground">
                    Требуется проверка инженером.
                  </div>
                )
              ) : null}
            </div>

            {!terminal ? (
              <LoadingState label="Backend продолжает обработку файла..." />
            ) : (
              <Tabs defaultValue="items" className="p-5">
                <TabsList>
                  <TabsTrigger value="items">
                    <FileText />
                    Позиции ({itemsQuery.data?.total ?? status.item_count})
                  </TabsTrigger>
                  <TabsTrigger value="issues">
                    <AlertTriangle />
                    Замечания ({issuesQuery.data?.total ?? status.errors_count + status.warnings_count})
                  </TabsTrigger>
                </TabsList>
                <TabsContent value="items">
                  {itemsQuery.isLoading ? (
                    <LoadingState label="Загрузка позиций..." />
                  ) : itemsQuery.isError ? (
                    <ErrorState label={extractApiError(itemsQuery.error)} />
                  ) : items.length ? (
                    <>
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Наименование</TableHead>
                            <TableHead>Артикул</TableHead>
                            <TableHead>Категория</TableHead>
                            <TableHead>Ед.</TableHead>
                            <TableHead>Цена</TableHead>
                            <TableHead>Характеристики</TableHead>
                            <TableHead>Источник</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {items.map((item) => (
                            <TableRow key={item.id}>
                              <TableCell className="font-semibold">
                                {item.name}
                              </TableCell>
                              <TableCell>{item.article ?? "—"}</TableCell>
                              <TableCell>
                                {item.category ?? "Не назначена"}
                              </TableCell>
                              <TableCell>{item.unit ?? "—"}</TableCell>
                              <TableCell>
                                {item.prices.length > 1 ? (() => {
                                  const seenKeys = new Map<string, number>();
                                  const options = item.prices.map((price) => {
                                    const baseKey = importPriceKey(price);
                                    const occurrence = seenKeys.get(baseKey) ?? 0;
                                    seenKeys.set(baseKey, occurrence + 1);
                                    return {
                                      key: occurrence ? `${baseKey}:${occurrence}` : baseKey,
                                      label: importPriceLabel(price),
                                      price,
                                    };
                                  });
                                  const selectedKey = selectedPrices[item.id] ?? options[0].key;
                                  const selected = options.find((option) => option.key === selectedKey) ?? options[0];
                                  return (
                                    <div className="min-w-48 space-y-1">
                                      <select
                                        className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                                        aria-label={`Выбрать цену для ${item.name}`}
                                        value={selected.key}
                                        onChange={(event) => setSelectedPrices((current) => ({
                                          ...current,
                                          [item.id]: event.target.value,
                                        }))}
                                      >
                                        {options.map((option) => (
                                          <option value={option.key} key={option.key}>{option.label}</option>
                                        ))}
                                      </select>
                                      <div className="text-sm tabular-nums">{formatImportPriceDetails(selected.price)}</div>
                                    </div>
                                  );
                                })() : item.prices[0] ? (
                                  <div>
                                    <div className="tabular-nums">{formatImportPriceDetails(item.prices[0])}</div>
                                    {item.prices[0].source_label ? (
                                      <div className="text-xs text-muted-foreground">{item.prices[0].source_label}</div>
                                    ) : null}
                                  </div>
                                ) : item.price}
                              </TableCell>
                              <TableCell className="max-w-80">
                                {item.attributes.length
                                  ? item.attributes.slice(0, 4).join("; ")
                                  : "—"}
                              </TableCell>
                              <TableCell>{item.source}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                      <Pagination
                        offset={itemsOffset}
                        total={itemsQuery.data?.total ?? 0}
                        onChange={setItemsOffset}
                      />
                    </>
                  ) : (
                    <div className="empty-state">
                      Сервер не вернул разобранных позиций
                    </div>
                  )}
                </TabsContent>
                <TabsContent value="issues">
                  {issuesQuery.isLoading ? (
                    <LoadingState label="Загрузка замечаний..." />
                  ) : issuesQuery.isError ? (
                    <ErrorState label={extractApiError(issuesQuery.error)} />
                  ) : issues.length ? (
                    <>
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Уровень</TableHead>
                            <TableHead>Код</TableHead>
                            <TableHead>Место</TableHead>
                            <TableHead>Исходное значение</TableHead>
                            <TableHead>Сообщение</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {issues.map((issue) => (
                            <TableRow key={issue.id}>
                              <TableCell>
                                <StatusBadge
                                  label={
                                    issue.severity === "error"
                                      ? "Ошибка"
                                      : "Предупреждение"
                                  }
                                  tone={
                                    issue.severity === "error"
                                      ? "danger"
                                      : "warning"
                                  }
                                />
                              </TableCell>
                              <TableCell className="font-mono text-xs">
                                {issue.code}
                              </TableCell>
                              <TableCell>{issue.location || "—"}</TableCell>
                              <TableCell>{issue.sourceValue ?? "—"}</TableCell>
                              <TableCell>{issue.message}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                      <Pagination
                        offset={issuesOffset}
                        total={issuesQuery.data?.total ?? 0}
                        onChange={setIssuesOffset}
                      />
                    </>
                  ) : (
                    <div className="empty-state">
                      Ошибок и предупреждений нет
                    </div>
                  )}
                </TabsContent>
              </Tabs>
            )}
          </>
        )}
      </Panel>

      <Dialog open={uploadOpen} onOpenChange={(open) => {
        if (open) setUploadOpen(true);
        else closeUpload();
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <Upload />
              Загрузить прайс
            </DialogTitle>
            <DialogDescription>
              Файл будет передан в проект «{project.name}» и обработан асинхронно.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={(event) => void submitImport(event)}>
            <div className="grid gap-2">
              <Label htmlFor="live-price-file">Файл</Label>
              <Input
                id="live-price-file"
                type="file"
                accept=".xlsx,.xlsm,.xls,.csv,.pdf,.docx,.doc,.txt"
                required
                onChange={(event) => {
                  setFile(event.target.files?.[0] ?? null);
                  setIdempotencyKey(newIdempotencyKey());
                }}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="live-supplier-code">Код поставщика</Label>
              <Input
                id="live-supplier-code"
                value={supplierExternalId}
                maxLength={255}
                placeholder="например, supplier-42"
                required
                onChange={(event) => setSupplierExternalId(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="live-supplier-name">Название поставщика</Label>
              <Input
                id="live-supplier-name"
                value={supplierName}
                maxLength={255}
                placeholder="Если пусто, используется код"
                onChange={(event) => setSupplierName(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="live-currency">Валюта, если не указана в файле</Label>
              <select
                id="live-currency"
                className="filter-select"
                value={currencyHint}
                onChange={(event) => setCurrencyHint(event.target.value)}
              >
                <option value="">Не подставлять</option>
                {["RUB", "KZT", "USD", "EUR", "CNY", "BYN", "UZS"].map(
                  (currency) => (
                    <option key={currency} value={currency}>
                      {currency}
                    </option>
                  ),
                )}
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="live-llm-provider">Провайдер разбора</Label>
              <select
                id="live-llm-provider"
                className="filter-select"
                value={provider}
                disabled={llmProfilesQuery.isLoading || !availableLlmProfiles.length}
                onChange={(event) => {
                  const nextProfile = availableLlmProfiles.find(
                    (profile) => profile.provider === event.target.value,
                  );
                  if (!nextProfile) return;
                  setProvider(nextProfile.provider);
                  setModel(nextProfile.default_model);
                }}
              >
                <option value="" disabled>
                  {llmProfilesQuery.isLoading
                    ? "Загрузка профилей..."
                    : "Нет доступных провайдеров"}
                </option>
                {availableLlmProfiles.map((profile) => (
                  <option key={profile.provider} value={profile.provider}>
                    {profile.label}
                  </option>
                ))}
              </select>
              {llmProfilesQuery.isError ? (
                <p className="text-sm text-destructive">
                  {extractApiError(llmProfilesQuery.error)}
                </p>
              ) : null}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="live-llm-model">Модель разбора</Label>
              <select
                id="live-llm-model"
                className="filter-select"
                value={model}
                disabled={!selectedLlmProfile}
                onChange={(event) => setModel(event.target.value)}
              >
                <option value="" disabled>
                  Выберите модель
                </option>
                {selectedLlmProfile?.models.map((availableModel) => (
                  <option key={availableModel} value={availableModel}>
                    {availableModel}
                  </option>
                ))}
              </select>
            </div>
            {!llmProfilesQuery.isLoading &&
            !llmProfilesQuery.isError &&
            availableLlmProfiles.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                LLM не настроен: разбор по готовым правилам/стаб; новый формат потребует ручной проверки.
              </p>
            ) : null}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeUpload}>
                Отмена
              </Button>
              <Button
                type="submit"
                disabled={createImport.isPending}
              >
                {createImport.isPending ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <Upload />
                )}
                Загрузить и разобрать
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
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
  if (total <= LIVE_PAGE_SIZE) return null;
  const from = offset + 1;
  const to = Math.min(offset + LIVE_PAGE_SIZE, total);
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
          onClick={() => onChange(Math.max(0, offset - LIVE_PAGE_SIZE))}
          aria-label="Предыдущая страница"
        >
          <ChevronLeft />
        </Button>
        <Button
          type="button"
          size="icon"
          variant="outline"
          disabled={offset + LIVE_PAGE_SIZE >= total}
          onClick={() => onChange(offset + LIVE_PAGE_SIZE)}
          aria-label="Следующая страница"
        >
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}





