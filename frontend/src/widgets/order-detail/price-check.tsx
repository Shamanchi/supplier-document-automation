import {
  Check,
  Globe2,
  Info,
  PenLine,
  Search,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  useAssembleQuote,
  useOrderLineOffers,
  useSuppliers,
  useUpdateQuote,
} from "@/shared/api/hooks";
import { startSmartRuleSearch } from "@/shared/api/resources";
import { formatMoney } from "@/shared/lib/utils";
import type {
  LineOffer,
  OrderDetail,
  QuoteLineSelection,
} from "@/shared/model/types";
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
  StatusBadge,
} from "@/shared/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import { FileViewer } from "@/widgets/order-detail/file-viewer";

/** Выбор менеджера для одной строки заявки: предложение прайса + количество. */
interface LineChoice {
  offer: LineOffer;
  quantity: number;
}

/**
 * Пикер предложений по строке (задача №7, дополнение): поиск по имени
 * позиции, а не по дефисному обозначению. Менеджер видит весь ряд
 * исполнений активных прайсов и выбирает сам — автоподбор не отсекает
 * ничего за низкий балл.
 */
function LineOfferPickerDialog({
  orderId,
  lineId,
  product,
  onClose,
  onConfirm,
}: {
  orderId: string;
  lineId: string;
  product: string;
  onClose: () => void;
  onConfirm: (choice: LineChoice) => void;
}) {
  const { data, isLoading, isError } = useOrderLineOffers(orderId, lineId);
  const [selected, setSelected] = useState<LineOffer | null>(null);
  const [quantity, setQuantity] = useState("1");

  const confirm = () => {
    if (!selected) return;
    const parsed = Number(quantity.replace(",", "."));
    onConfirm({
      offer: selected,
      quantity: Number.isFinite(parsed) && parsed > 0 ? parsed : 1,
    });
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Выбор позиции прайса</DialogTitle>
          <DialogDescription>
            Поиск по имени позиции «{product}»: предложения из активных
            прайсов всех поставщиков
          </DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <LoadingState label="Поиск предложений..." />
        ) : isError ? (
          <ErrorState label="Не удалось загрузить предложения" />
        ) : !data || data.offers.length === 0 ? (
          <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
            <Info className="size-4" />
            По этой позиции ничего не нашлось. Попробуйте уточнить
            наименование строки заявки.
          </div>
        ) : (
          <div className="max-h-[50vh] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Поставщик</TableHead>
                  <TableHead>Наименование</TableHead>
                  <TableHead>Ед.</TableHead>
                  <TableHead>Цена</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.offers.map((offer) => (
                  <TableRow
                    key={offer.priceItemId}
                    className="cursor-pointer"
                    data-state={
                      selected?.priceItemId === offer.priceItemId
                        ? "selected"
                        : undefined
                    }
                    onClick={() => setSelected(offer)}
                  >
                    <TableCell className="font-semibold">
                      {offer.supplier}
                    </TableCell>
                    <TableCell>
                      <div>{offer.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {offer.canonicalTitle}
                      </div>
                    </TableCell>
                    <TableCell>{offer.unit}</TableCell>
                    <TableCell className="font-semibold">
                      {offer.price === null ? "—" : formatMoney(offer.price)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="grid gap-1">
          <Label htmlFor="offer-quantity">Количество</Label>
          <Input
            className="w-28"
            id="offer-quantity"
            inputMode="decimal"
            onChange={(event) => setQuantity(event.target.value)}
            value={quantity}
          />
        </div>
        <DialogFooter>
          <Button onClick={onClose} variant="outline">
            Отмена
          </Button>
          <Button disabled={!selected} onClick={confirm}>
            <Check />
            Выбрать
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function PriceCheck({ order }: { order: OrderDetail }) {
  const updateQuote = useUpdateQuote();
  const assembleQuote = useAssembleQuote();
  const { data: suppliers } = useSuppliers();
  // Выборы менеджера по строкам: ключ — id строки заявки (у неподобранной
  // позиции вкладки он и есть id строки расчётного листа).
  const [choices, setChoices] = useState<Record<string, LineChoice>>({});
  const [picker, setPicker] = useState<{
    lineId: string;
    product: string;
  } | null>(null);
  const priceFiles = order.files.filter((file) => file.kind === "price");
  const searchableFile = priceFiles.find((file) => file.priceVersionId);
  const hasUnresolvedPrices = order.pricePositions.some(
    (position) => position.status !== "success",
  );
  const canPick = !order.readOnly;

  const runSmartSearch = async () => {
    if (!searchableFile?.priceVersionId) {
      toast.info(
        "Прайс ещё не связан с импортированной версией. Смарт-поиск станет доступен после завершения импорта.",
      );
      return;
    }
    try {
      const batch = await startSmartRuleSearch(searchableFile.priceVersionId, {
        provider: "deepseek",
        model: "deepseek-v4-flash",
      });
      toast.success(`Смарт-поиск запущен: пакет ${batch.batch_id}`);
    } catch {
      toast.error("Не удалось запустить смарт-поиск структуры прайса");
    }
  };

  const assemble = async () => {
    const selections: QuoteLineSelection[] = Object.entries(choices).map(
      ([lineId, choice]) => ({
        lineId,
        canonicalId: choice.offer.canonicalId,
        priceItemId: choice.offer.priceItemId,
        quantity: choice.quantity,
      }),
    );
    const result = await assembleQuote.mutateAsync({
      orderId: order.id,
      selections,
    });
    const skipped = result.skipped ?? [];
    if (skipped.length) {
      toast.warning(
        `КП собрано, но пропущено строк: ${skipped.length}. Причины: ${skipped
          .map((item) => item.reason)
          .join("; ")}`,
      );
    } else {
      toast.success("КП собрано: расчёт создан по выбранным позициям");
    }
    setChoices({});
  };

  return (
    <div>
      <div className="rule-hint mb-3">
        <Info className="size-4" />
        Отображаются только позиции, прошедшие проверку ТХ
      </div>
      <div className="mb-2 font-semibold">Файлы для проверки цен</div>
      <div className="grid gap-2">
        {priceFiles.map((file) => (
            <FileViewer file={file} key={file.id} />
          ))}
      </div>
      <div className="mb-2 mt-4 font-semibold">Таблица цен</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Позиция</TableHead>
            <TableHead>Ед.</TableHead>
            <TableHead>Поставщик</TableHead>
            <TableHead>Цена</TableHead>
            <TableHead>Скидка</TableHead>
            <TableHead>Метод</TableHead>
            <TableHead>Статус</TableHead>
            <TableHead>Источник</TableHead>
            <TableHead>Действия</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {order.pricePositions.map((position) => {
            const choice = choices[position.id];
            return (
            <TableRow key={position.id}>
              <TableCell className="font-semibold">{position.product}</TableCell>
              <TableCell>{choice ? choice.offer.unit : position.unit}</TableCell>
              <TableCell>
                {choice ? choice.offer.supplier : position.supplier}
              </TableCell>
              <TableCell
                className="font-semibold"
                style={{
                  color:
                    position.selected || choice ? "var(--ok)" : undefined,
                }}
              >
                {choice
                  ? choice.offer.price === null
                    ? "—"
                    : formatMoney(choice.offer.price)
                  : formatMoney(position.price)}
              </TableCell>
              <TableCell>{position.discount}%</TableCell>
              <TableCell>
                <span
                  className="flex items-center gap-1"
                  style={{
                    color:
                      position.method === "auto" ? "var(--ok)" : "var(--warn)",
                  }}
                >
                  {position.method === "auto" ? (
                    <Check className="size-4" />
                  ) : (
                    <PenLine className="size-4" />
                  )}
                  {position.method === "auto" ? "Авто" : "Ручной"}
                </span>
              </TableCell>
              <TableCell>
                <StatusBadge
                  label={
                    choice
                      ? `Выбрано ×${choice.quantity}`
                      : position.status === "success"
                        ? "Подтверждён"
                        : "Требует проверки"
                  }
                  tone={
                    choice || position.status === "success"
                      ? "success"
                      : "warning"
                  }
                />
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {position.source}
              </TableCell>
              <TableCell>
                <div className="button-row">
                  {position.status !== "success" && canPick ? (
                    <Button
                      onClick={() =>
                        setPicker({
                          lineId: position.id,
                          product: position.product,
                        })
                      }
                      size="sm"
                      variant="outline"
                    >
                      <Search />
                      {choice ? "Изменить выбор" : "Выбрать позицию"}
                    </Button>
                  ) : null}
                  <Button
                    size="sm"
                    variant={position.selected ? "success" : "default"}
                    onClick={async () => {
                      const supplier = suppliers?.find(
                        (item) => item.name === position.supplier,
                      );
                      await updateQuote.mutateAsync({
                        orderId: order.id,
                        payload: { supplierId: supplier?.id ?? "s-1" },
                      });
                      toast.success(`Выбран поставщик: ${position.supplier}`);
                    }}
                  >
                    <Check />
                    {position.selected ? "Выбран" : "Выбрать"}
                  </Button>
                </div>
              </TableCell>
            </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <div className="action-row mt-3">
        {canPick ? (
          <Button
            disabled={!Object.keys(choices).length || assembleQuote.isPending}
            onClick={() => void assemble()}
          >
            <Check />
            Собрать КП ({Object.keys(choices).length})
          </Button>
        ) : null}
        <Button
          variant="orange"
          disabled={!hasUnresolvedPrices}
          onClick={() => void runSmartSearch()}
        >
          <Globe2 />
          Смарт-поиск структуры
        </Button>
      </div>
      {picker ? (
        <LineOfferPickerDialog
          lineId={picker.lineId}
          onClose={() => setPicker(null)}
          onConfirm={(choice) => {
            setChoices((previous) => ({ ...previous, [picker.lineId]: choice }));
            setPicker(null);
          }}
          orderId={order.id}
          product={picker.product}
        />
      ) : null}
    </div>
  );
}
