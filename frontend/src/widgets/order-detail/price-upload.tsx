import { Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { extractApiError } from "@/shared/api/client";
import { useUploadOrderPriceDocument } from "@/shared/api/hooks";
import type { OrderPriceDocumentType } from "@/shared/api/backend-types";
import type { OrderCard } from "@/shared/model/types";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

/**
 * Мостик «заявка → прайс/счёт» (Ф2→Ф3).
 *
 * Существующий импорт из сделки (`BitrixImportButton`) работает только у заявок,
 * связанных с B24: у локальной заявки, цена которой пришла письмом или счётом,
 * не было никакого способа попасть в контур цен. Эта кнопка закрывает дыру —
 * файл уходит тем же путём (Ф2: файл → версия → разбор), но заводится из
 * карточки заявки.
 */

/** Назначение решает судьбу документа, поэтому подписи — «что будет дальше». */
const DOCUMENT_TYPE_LABELS: Record<OrderPriceDocumentType, string> = {
  PRICE_LIST: "Прайс-лист (общий каталог)",
  SUPPLIER_QUOTE: "Счёт/КП для этой заявки",
};

const DOCUMENT_TYPE_HINTS: Record<OrderPriceDocumentType, string> = {
  PRICE_LIST:
    "Общий прайс поставщика появится в подборе только после активации администратором в инженерном интерфейсе.",
  SUPPLIER_QUOTE:
    "Счёт/КП действует только в этой заявке и появится в подборе после разбора и проверки. Активировать его нельзя.",
};

/**
 * Пустая строка для Radix Select запрещена, поэтому «не подставлять» —
 * отдельное значение; в запрос оно не попадает.
 */
const CURRENCY_AUTO = "AUTO";

const CURRENCIES = ["RUB", "KZT", "USD", "EUR", "CNY", "BYN", "UZS"];

function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") {
    return `ui-${crypto.randomUUID()}`;
  }
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function PriceUploadDialog({
  order,
  open,
  onOpenChange,
}: {
  order: OrderCard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const upload = useUploadOrderPriceDocument();
  const [file, setFile] = useState<File | null>(null);
  const [supplierExternalId, setSupplierExternalId] = useState("");
  const [supplierName, setSupplierName] = useState("");
  const [documentType, setDocumentType] =
    useState<OrderPriceDocumentType>("PRICE_LIST");
  const [currency, setCurrency] = useState(CURRENCY_AUTO);
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey);

  // Закрытие диалога выбрасывает состояние: следующий документ — это другой
  // файл и другой поставщик, а остатки прошлой загрузки менеджер не заметит.
  // Ключ идемпотентности обновляется вместе с ними, иначе повторная отправка
  // другого файла под старым ключом вернула бы чужой импорт.
  useEffect(() => {
    if (open) return;
    setFile(null);
    setSupplierExternalId("");
    setSupplierName("");
    setDocumentType("PRICE_LIST");
    setCurrency(CURRENCY_AUTO);
    setIdempotencyKey(newIdempotencyKey());
  }, [open]);

  const submit = async () => {
    const externalId = supplierExternalId.trim();
    if (!file) {
      toast.error("Выберите файл прайса или счёта");
      return;
    }
    if (!externalId) {
      toast.error("Укажите код поставщика");
      return;
    }

    try {
      const accepted = await upload.mutateAsync({
        batchId: order.id,
        file,
        supplierExternalId: externalId,
        supplierName: supplierName.trim() || undefined,
        documentType,
        currencyHint: currency === CURRENCY_AUTO ? undefined : currency,
        idempotencyKey,
      });
      onOpenChange(false);
      toast.success(
        accepted.replayed
          ? "Этот документ уже принят в обработку"
          : "Документ принят в обработку",
      );
    } catch (cause) {
      // Причины отказа разные (формат, пустой файл, нет поставщика, квота):
      // показываем сообщение backend'а, а не свою догадку.
      toast.error(extractApiError(cause));
    }
  };

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Загрузить прайс или счёт</DialogTitle>
          <DialogDescription>
            Документ по заявке №{order.number}. Файл разбирается асинхронно:
            позиции появятся в подборе заявки после разбора и проверки.
          </DialogDescription>
        </DialogHeader>

        <form
          className="dialog-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="grid gap-2">
            <Label htmlFor="price-upload-file">Файл</Label>
            <Input
              accept=".pdf,.xlsx,.docx"
              id="price-upload-file"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              required
              type="file"
            />
            <div className="text-xs text-muted-foreground">
              Поддерживаются PDF, XLSX и DOCX — формат проверяется по
              содержимому файла, а не по имени.
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="price-upload-supplier">Код поставщика</Label>
            <Input
              id="price-upload-supplier"
              maxLength={255}
              onChange={(event) => setSupplierExternalId(event.target.value)}
              placeholder="например, supplier-42"
              required
              value={supplierExternalId}
            />
            <div className="text-xs text-muted-foreground">
              Если такого поставщика ещё нет, он будет заведён под этим кодом.
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="price-upload-supplier-name">
              Название поставщика
            </Label>
            <Input
              id="price-upload-supplier-name"
              maxLength={255}
              onChange={(event) => setSupplierName(event.target.value)}
              placeholder="Если пусто, используется код"
              value={supplierName}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="price-upload-document-type">Назначение</Label>
            <Select
              onValueChange={(value) =>
                setDocumentType(value as OrderPriceDocumentType)
              }
              value={documentType}
            >
              <SelectTrigger
                aria-label="Назначение документа"
                id="price-upload-document-type"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(
                  Object.keys(DOCUMENT_TYPE_LABELS) as OrderPriceDocumentType[]
                ).map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {DOCUMENT_TYPE_LABELS[kind]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="text-xs text-muted-foreground">
              {DOCUMENT_TYPE_HINTS[documentType]}
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="price-upload-currency">
              Валюта, если не указана в файле
            </Label>
            <Select onValueChange={setCurrency} value={currency}>
              <SelectTrigger
                aria-label="Валюта документа"
                id="price-upload-currency"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={CURRENCY_AUTO}>Не подставлять</SelectItem>
                {CURRENCIES.map((code) => (
                  <SelectItem key={code} value={code}>
                    {code}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* НДС здесь не спрашиваем: ставка — свойство поставщика, а не файла,
              и вводится на странице «Поставщики», куда импорт и встанет после
              разбора. Спрашивать её дважды — заводить два разных ответа. */}
          <div className="text-xs text-muted-foreground">
            Ставку НДС диалог не спрашивает: если её нет в файле, импорт после
            разбора попросит её на странице «Поставщики».
          </div>

          <DialogFooter>
            <Button
              onClick={() => onOpenChange(false)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button disabled={upload.isPending || !file} type="submit">
              <Upload />
              Загрузить
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Кнопка загрузки ценового документа на карточке заявки.
 *
 * Показывается для всех заявок, которые менеджер ведёт (`canWrite`), а не
 * только связанных со сделкой B24 — в этом и смысл мостика. Файл уходит в
 * контур Ф2, поэтому результат виден не сразу: подсказка в диалоге говорит,
 * когда документ попадёт в подбор.
 */
export function PriceUploadButton({
  order,
  canWrite,
}: {
  order: OrderCard;
  canWrite: boolean;
}) {
  const [open, setOpen] = useState(false);

  if (!canWrite) return null;

  return (
    <>
      <Button onClick={() => setOpen(true)} size="sm" variant="outline">
        <Upload />
        Загрузить прайс/счёт
      </Button>
      <PriceUploadDialog onOpenChange={setOpen} open={open} order={order} />
    </>
  );
}
