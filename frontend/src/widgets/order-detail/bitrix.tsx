import { ExternalLink, FolderDown, RotateCcw, Send } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { useQueryClient } from "@tanstack/react-query";

import {
  useBitrixHealth,
  useBitrixJob,
  useBitrixKitStatus,
  useImportBitrixFiles,
  usePrepareBitrixDeal,
  usePublishToBitrix,
  useRetryBitrixJob,
  useSuppliers,
} from "@/shared/api/hooks";
import { formatBytes } from "@/shared/lib/utils";
import type {
  BitrixDealFile,
  BitrixJob,
  BitrixJobStatus,
  DocumentRole,
  FileKind,
  KitStatus,
  OrderCard,
  PriceDocumentKind,
  ReportArtifact,
} from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { Checkbox } from "@/shared/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/dialog";
import { ErrorState, LoadingState, StatusBadge, type StatusTone } from "@/shared/ui/page";
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

const JOB_LABELS: Record<BitrixJobStatus, string> = {
  QUEUED: "В очереди",
  RUNNING: "Выполняется",
  RETRY_SCHEDULED: "Повтор запланирован",
  COMPLETED: "Готово",
  FAILED: "Ошибка",
  CANCELLED: "Отменено",
};

const JOB_TONE: Record<BitrixJobStatus, StatusTone> = {
  QUEUED: "info",
  RUNNING: "info",
  RETRY_SCHEDULED: "warning",
  COMPLETED: "success",
  FAILED: "danger",
  CANCELLED: "archived",
};

const KIND_LABELS: Record<FileKind, string> = {
  REQUIREMENT: "Требования (ТЗ)",
  FACT: "Факт (паспорт)",
  PRICE: "Ценовой документ",
};

const PRICE_DOCUMENT_LABELS: Record<PriceDocumentKind, string> = {
  PRICE_LIST: "Общий прайс поставщика",
  SUPPLIER_QUOTE: "Разовый счёт/КП заявки",
};

/** Статус комплекта заявки: связь, документы Ф3 и ценовые документы Ф2. */
const KIT_STATUS_LABELS: Record<KitStatus, string> = {
  BITRIX_NOT_LINKED: "Локальная заявка",
  BITRIX_DEAL_FOUND: "Сделка найдена",
  FILES_AVAILABLE: "Файлы доступны",
  DOCUMENTS_IMPORTED: "Документы импортированы",
  PRICE_IMPORT_RUNNING: "Обработка прайса",
  PRICE_REVIEW_REQUIRED: "Прайс требует проверки",
  PRICE_READY: "Прайс готов",
  READY_FOR_CALCULATION: "Готово к расчёту",
};

const KIT_STATUS_TONE: Record<KitStatus, StatusTone> = {
  BITRIX_NOT_LINKED: "archived",
  BITRIX_DEAL_FOUND: "info",
  FILES_AVAILABLE: "info",
  DOCUMENTS_IMPORTED: "warning",
  PRICE_IMPORT_RUNNING: "info",
  PRICE_REVIEW_REQUIRED: "warning",
  PRICE_READY: "success",
  READY_FOR_CALCULATION: "success",
};

/** Откуда файл взят в сделке — показываем, чтобы менеджер оценил доверие. */
const SOURCE_LABELS: Record<string, string> = {
  DEAL_FIELD: "Поле сделки",
  DISK: "Диск",
  TIMELINE: "Таймлайн",
};

function formatMoment(value: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

/**
 * Состояние внешней операции.
 *
 * Отдельная строка, а не toast: задание живёт дольше одного нажатия, у него
 * бывают повторы, и менеджеру нужно видеть, чем оно кончилось.
 */
function JobLine({
  job,
  onRetry,
  retrying,
}: {
  job: BitrixJob;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const retryable = job.error?.retryable === true;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border p-3 text-sm">
      <StatusBadge label={JOB_LABELS[job.status]} tone={JOB_TONE[job.status]} />
      <span className="text-muted-foreground">Попытка {job.attempt}</span>
      {job.error ? (
        <span className="text-danger">
          {job.error.message ?? job.error.code}
        </span>
      ) : null}
      {job.status === "RETRY_SCHEDULED" && job.nextRetryAt ? (
        <span className="text-muted-foreground">
          Повтор в {formatMoment(job.nextRetryAt)}
        </span>
      ) : null}
      {onRetry && job.status === "FAILED" && retryable ? (
        <Button disabled={retrying} onClick={onRetry} size="sm" variant="outline">
          <RotateCcw />
          Повторить
        </Button>
      ) : null}
    </div>
  );
}

interface Selection {
  kind: FileKind;
  itemId: string;
  priceDocumentType: PriceDocumentKind;
  supplierId: string;
}

/** Подтверждённый тип: подсказка сделки, иначе безопасное значение по умолчанию. */
function defaultKind(file: BitrixDealFile): FileKind {
  return file.suggestedKind ?? file.suggestedRole ?? "REQUIREMENT";
}

/**
 * Импорт файлов сделки в позиции заявки и в ценовой контур.
 *
 * Тип и назначение выбирает менеджер: `suggestedKind` приходит из имени файла и
 * является подсказкой, а не решением (контракт §Список файлов). Документ Ф3
 * привязывается к позиции, ценовой документ Ф2 — к поставщику и назначению
 * (общий прайс или разовый счёт заявки). Уже импортированные файлы показываются,
 * но выбрать их нельзя — повторный импорт backend отклонит.
 */
function ImportDialog({
  order,
  open,
  onOpenChange,
}: {
  order: OrderCard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const tenderId = order.tenderId ?? null;
  const queryClient = useQueryClient();
  const prepare = usePrepareBitrixDeal();
  const suppliers = useSuppliers();
  const runImport = useImportBitrixFiles();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useBitrixJob(jobId, "imports");
  const [selection, setSelection] = useState<Record<string, Selection>>({});

  useEffect(() => {
    if (!open || !tenderId || prepare.isPending || prepare.data || prepare.isError) return;
    void prepare.mutateAsync({
      tenderId,
      orderId: order.id,
      items: order.items.map((item) => ({
        positionNo: item.positionNo,
        name: item.name,
        supplier: order.supplier,
      })),
    }).catch(() => undefined);
  }, [open, tenderId, order.id, order.items, order.supplier, prepare]);

  useEffect(() => {
    if (!open) {
      prepare.reset();
      setSelection({});
      setJobId(null);
    }
  }, [open, prepare]);

  const files = prepare.data?.files ?? [];

  const firstItemId = order.items[0]?.id ?? "";
  const chosen = useMemo(
    () =>
      Object.entries(selection).map(([externalFileId, value]) =>
        value.kind === "PRICE"
          ? {
              externalFileId,
              kind: "PRICE" as const,
              priceDocumentType: value.priceDocumentType,
              supplierId: value.supplierId,
            }
          : {
              externalFileId,
              kind: value.kind,
              checkItemId: value.itemId,
              role: value.kind as DocumentRole,
            },
      ),
    [selection],
  );
  // Ценовой документ без поставщика backend отклонит с `SUPPLIER_REQUIRED`,
  // поэтому кнопку импорта держим выключенной, а не ловим 422 после отправки.
  const priceWithoutSupplier = chosen.some(
    (file) => file.kind === "PRICE" && !file.supplierId,
  );

  const finished = job.data?.status === "COMPLETED";

  // Импорт закончился успешно: документы позиций перечитываются, иначе список
  // файлов на карточке остался бы прежним до перезагрузки страницы. Эффект, а
  // не проверка в рендере: сброс состояния прямо в теле компонента запускает
  // повторный рендер на каждом опросе задания.
  useEffect(() => {
    if (!finished) return;
    setJobId(null);
    setSelection({});
    void queryClient.invalidateQueries({ queryKey: ["check-items"] });
    void queryClient.invalidateQueries({ queryKey: ["orders", order.id] });
    void queryClient.invalidateQueries({
      queryKey: ["integrations", "bitrix", "deals"],
    });
    void queryClient.invalidateQueries({
      queryKey: ["integrations", "bitrix", "check-batches"],
    });
    toast.success("Файлы импортированы из сделки");
  }, [finished, order.id, queryClient]);

  const toggle = (file: BitrixDealFile) => {
    setSelection((current) => {
      const next = { ...current };
      if (next[file.externalFileId]) {
        delete next[file.externalFileId];
      } else {
        next[file.externalFileId] = {
          kind: defaultKind(file),
          itemId: firstItemId,
          priceDocumentType: "PRICE_LIST",
          supplierId: "",
        };
      }
      return next;
    });
  };

  const update = (externalFileId: string, patch: Partial<Selection>) => {
    setSelection((current) =>
      current[externalFileId]
        ? { ...current, [externalFileId]: { ...current[externalFileId], ...patch } }
        : current,
    );
  };

  const submit = async () => {
    if (!tenderId || priceWithoutSupplier) return;
    try {
      const accepted = await runImport.mutateAsync({
        tenderId,
        orderId: order.id,
        files: chosen,
      });
      setJobId(accepted.jobId);
      toast.info(`Импорт поставлен в очередь: файлов ${accepted.filesTotal}`);
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось запустить импорт",
      );
    }
  };

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Импорт файлов из сделки</DialogTitle>
          <DialogDescription>
            {prepare.data?.deal
              ? `${prepare.data.deal.title ?? "Сделка"} · ${prepare.data.deal.customer ?? "заказчик не указан"}`
              : `Тендер ${tenderId ?? "—"}`}
          </DialogDescription>
        </DialogHeader>

        {prepare.data?.deal.externalUrl ? (
          <a
            className="inline-flex w-fit items-center gap-1.5 text-sm text-primary hover:underline"
            href={prepare.data.deal.externalUrl}
            rel="noreferrer"
            target="_blank"
          >
            <ExternalLink className="size-3.5" />
            Открыть сделку в Bitrix24
          </a>
        ) : null}

        {prepare.isPending ? (
          <LoadingState label="Читаем сделку..." />
        ) : null}

        {prepare.isError ? (
          <ErrorState
            label={
              prepare.error instanceof Error
                ? prepare.error.message
                : "Не удалось получить файлы сделки"
            }
          />
        ) : null}

        {prepare.data?.notes.length ? (
          <div className="grid gap-1 rounded-md border border-border p-3 text-sm">
            <div className="text-xs text-muted-foreground">
              Лента сделки — здесь Фаза 1 перечисляет вложения, которые не
              смогла загрузить на портал
            </div>
            {prepare.data.notes.map((note) => (
              <div className="whitespace-pre-line" key={note}>
                {note}
              </div>
            ))}
          </div>
        ) : null}

        {prepare.data && files.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            В сделке нет доступных файлов. Проверьте, что документы приложены к
            сделке и что источник файлов настроен в интеграции.
          </div>
        ) : null}

        {files.length ? (
          <div className="max-h-[45vh] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10" />
                  <TableHead>Файл</TableHead>
                  <TableHead>Тип</TableHead>
                  <TableHead>Назначение</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((file) => {
                  const imported = file.alreadyImported;
                  const picked = selection[file.externalFileId];
                  return (
                    <TableRow key={file.externalFileId}>
                      <TableCell>
                        <Checkbox
                          aria-label={`Выбрать ${file.name}`}
                          checked={Boolean(picked)}
                          disabled={imported}
                          onCheckedChange={() => toggle(file)}
                        />
                      </TableCell>
                      <TableCell>
                        <div className="font-medium">{file.name}</div>
                        <div className="text-xs text-muted-foreground">
                          {formatBytes(file.sizeBytes)} ·{" "}
                          {file.mediaType ?? "тип неизвестен"} ·{" "}
                          {SOURCE_LABELS[file.source] ?? file.source}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {file.suggestedKind
                            ? `Подсказка: ${KIND_LABELS[file.suggestedKind]}${
                                file.kindConfidence
                                  ? `, уверенность ${file.kindConfidence}%`
                                  : ""
                              }`
                            : "Тип не распознан — выберите вручную"}
                          {imported ? " · уже импортирован" : ""}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Select
                          disabled={!picked}
                          onValueChange={(value) =>
                            update(file.externalFileId, {
                              kind: value as FileKind,
                            })
                          }
                          value={picked?.kind ?? defaultKind(file)}
                        >
                          <SelectTrigger aria-label={`Тип файла ${file.name}`}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {(Object.keys(KIND_LABELS) as FileKind[]).map(
                              (kind) => (
                                <SelectItem key={kind} value={kind}>
                                  {KIND_LABELS[kind]}
                                </SelectItem>
                              ),
                            )}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        {picked?.kind === "PRICE" ? (
                          <div className="grid gap-2">
                            <Select
                              onValueChange={(value) =>
                                update(file.externalFileId, {
                                  priceDocumentType: value as PriceDocumentKind,
                                })
                              }
                              value={picked.priceDocumentType}
                            >
                              <SelectTrigger
                                aria-label={`Назначение файла ${file.name}`}
                              >
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {(
                                  Object.keys(
                                    PRICE_DOCUMENT_LABELS,
                                  ) as PriceDocumentKind[]
                                ).map((kind) => (
                                  <SelectItem key={kind} value={kind}>
                                    {PRICE_DOCUMENT_LABELS[kind]}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            <Select
                              onValueChange={(value) =>
                                update(file.externalFileId, {
                                  supplierId: value,
                                })
                              }
                              value={picked.supplierId}
                            >
                              <SelectTrigger
                                aria-label={`Поставщик для ${file.name}`}
                              >
                                <SelectValue placeholder="Выберите поставщика" />
                              </SelectTrigger>
                              <SelectContent>
                                {(suppliers.data ?? []).map((supplier) => (
                                  <SelectItem
                                    key={supplier.id}
                                    value={supplier.id}
                                  >
                                    {supplier.name}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </div>
                        ) : (
                          <Select
                            disabled={!picked}
                            onValueChange={(value) =>
                              update(file.externalFileId, { itemId: value })
                            }
                            value={picked?.itemId ?? firstItemId}
                          >
                            <SelectTrigger
                              aria-label={`Позиция для ${file.name}`}
                            >
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {order.items.map((item) => (
                                <SelectItem key={item.id} value={item.id}>
                                  {item.positionNo}. {item.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        ) : null}

        {priceWithoutSupplier ? (
          <div className="text-sm text-warning">
            Для ценового документа выберите поставщика — без него backend
            отклонит импорт.
          </div>
        ) : null}

        {job.data ? <JobLine job={job.data} /> : null}

        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="outline">
            Закрыть
          </Button>
          <Button
            disabled={
              !chosen.length ||
              priceWithoutSupplier ||
              runImport.isPending ||
              Boolean(jobId)
            }
            onClick={submit}
          >
            <FolderDown />
            Импортировать{chosen.length ? ` (${chosen.length})` : ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Статус комплекта заявки: связь со сделкой, документы Ф3 и ценовые Ф2.
 *
 * Читается из локального снимка (`.../status`), поэтому после перезагрузки
 * страницы показывает то же состояние, что и до неё, даже когда Bitrix24
 * недоступен.
 */
export function BatchKitStatusBadge({
  orderId,
  linked = true,
}: {
  orderId: string;
  linked?: boolean;
}) {
  const kit = useBitrixKitStatus(orderId, linked);
  const status = kit.data?.status;
  if (!status || !(status in KIT_STATUS_LABELS)) return null;
  return (
    <StatusBadge label={KIT_STATUS_LABELS[status]} tone={KIT_STATUS_TONE[status]} />
  );
}

/**
 * Кнопка импорта на карточке заявки.
 *
 * Прячется, когда интеграция выключена или заявка не связана со сделкой:
 * действие, которое гарантированно ответит `503` или `404`, показывать нельзя.
 */
export function BitrixImportButton({
  order,
  canWrite,
}: {
  order: OrderCard;
  canWrite: boolean;
}) {
  const health = useBitrixHealth(Boolean(order.tenderId));
  const [open, setOpen] = useState(false);

  if (!canWrite || !order.tenderId || !health.data?.configured) return null;

  return (
    <>
      <Button onClick={() => setOpen(true)} size="sm" variant="outline">
        <FolderDown />
        Загрузить из B24
      </Button>
      <ImportDialog onOpenChange={setOpen} open={open} order={order} />
    </>
  );
}

/**
 * Публикация отчёта в сделку.
 *
 * Живёт рядом с артефактами отчёта: публиковать можно только то, что уже
 * собрано, а список артефактов известен именно там. Повторная публикация того
 * же артефакта отклоняется backend'ом (`REPORT_ALREADY_PUBLISHED`), поэтому
 * кнопка после успеха не предлагает отправить снова.
 */
export function BitrixPublishPanel({
  orderId,
  artifacts,
  canWrite,
  tenderId,
}: {
  orderId: string;
  artifacts: ReportArtifact[];
  canWrite: boolean;
  tenderId?: string | null;
}) {
  const health = useBitrixHealth(Boolean(tenderId));
  const publish = usePublishToBitrix();
  const retry = useRetryBitrixJob();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useBitrixJob(jobId, "publications");

  if (!canWrite || !artifacts.length || !tenderId || !health.data?.configured)
    return null;

  const send = async () => {
    try {
      const accepted = await publish.mutateAsync({
        orderId,
        reportArtifactIds: artifacts.map((artifact) => artifact.id),
      });
      setJobId(accepted.jobId);
      toast.info(`Публикация в сделку ${accepted.tenderId} поставлена в очередь`);
    } catch (cause) {
      // Предусловия проверяет backend: неутверждённый вердикт, неготовый
      // отчёт, отсутствие связи со сделкой. Причину показываем как есть.
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось отправить отчёт",
      );
    }
  };

  const current = job.data ?? null;
  const done = current?.status === "COMPLETED";

  return (
    <div className="grid gap-2">
      <div className="flex-between gap-3">
        <div className="text-sm text-muted-foreground">
          {done
            ? "Отчёт опубликован в сделке Bitrix24"
            : "Отчёт можно отправить в сделку Bitrix24"}
        </div>
        <Button
          disabled={publish.isPending || Boolean(jobId)}
          onClick={send}
          size="sm"
        >
          <Send />
          Отправить в B24
        </Button>
      </div>
      {current ? (
        <JobLine
          job={current}
          onRetry={() => {
            void retry.mutateAsync(current.id).catch((cause: unknown) => {
              toast.error(
                cause instanceof Error ? cause.message : "Повтор невозможен",
              );
            });
          }}
          retrying={retry.isPending}
        />
      ) : null}
    </div>
  );
}
