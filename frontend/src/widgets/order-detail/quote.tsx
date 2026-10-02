import {
  BadgePercent,
  Box,
  Check,
  Download,
  FileText,
  Info,
  PenLine,
  Scale,
  Send,
  WalletCards,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { useOrderAction, useSuppliers, useUpdateQuote } from "@/shared/api/hooks";
import { formatMoney } from "@/shared/lib/utils";
import type { OrderDetail } from "@/shared/model/types";
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
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/shared/ui/tooltip";

const reasonSchema = z.object({
  value: z.coerce.number().min(0).max(100),
  reason: z.string().min(5, "Укажите причину изменения"),
});

type ReasonValues = z.infer<typeof reasonSchema>;

export function Quote({ order }: { order: OrderDetail }) {
  const navigate = useNavigate();
  const updateQuote = useUpdateQuote();
  const orderAction = useOrderAction();
  const { data: suppliers } = useSuppliers();
  const [reasonType, setReasonType] = useState<"vat" | "discount" | null>(null);
  const reasonForm = useForm<ReasonValues>({
    resolver: zodResolver(reasonSchema),
    defaultValues: { value: order.vatRate, reason: "" },
  });

  const totals = useMemo(() => {
    const discounted = order.quotePositions.reduce(
      (sum, item) => sum + item.subtotal,
      0,
    );
    const specialAmount = Math.round(
      discounted * (order.specialDiscount / 100),
    );
    const vatBase = discounted - specialAmount;
    const vat = Math.round(vatBase * (order.vatRate / 100));
    return {
      gross: order.quotePositions.reduce(
        (sum, item) => sum + item.price * item.quantity,
        0,
      ),
      lineDiscount: order.quotePositions.reduce(
        (sum, item) => sum + item.discount,
        0,
      ),
      discounted,
      specialAmount,
      vatBase,
      vat,
      grand: vatBase + vat,
    };
  }, [order.quotePositions, order.specialDiscount, order.vatRate]);

  const openReason = (type: "vat" | "discount") => {
    reasonForm.reset({
      value: type === "vat" ? order.vatRate : order.specialDiscount,
      reason: "",
    });
    setReasonType(type);
  };

  const saveReason = reasonForm.handleSubmit(async ({ value, reason }) => {
    if (!reasonType) return;
    await updateQuote.mutateAsync({
      orderId: order.id,
      payload:
        reasonType === "vat"
          ? { vatRate: value, reason }
          : { specialDiscount: value, reason },
    });
    toast.success(
      reasonType === "vat"
        ? "Ставка НДС изменена"
        : "Специальная скидка изменена",
    );
    setReasonType(null);
  });

  return (
    <>
      <div className="rounded-md border border-border bg-muted/30 p-4">
        <div className="mb-3 flex items-center gap-2 text-base font-semibold">
          <FileText className="size-4" />
          КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ
        </div>
        <div className="kp-meta">
          <div>
            <span className="text-muted-foreground">Оборудование:</span>{" "}
            <strong>
              {order.quotePositions.map((item) => item.product).join(", ")}
            </strong>
          </div>
          <div>
            <span className="text-muted-foreground">Заказчик:</span>{" "}
            <strong>{order.client}</strong>
          </div>
          <div>
            <span className="text-muted-foreground">Поставщики:</span>{" "}
            <strong>
              {[...new Set(order.quotePositions.map((item) => item.supplier))].join(
                ", ",
              )}
            </strong>
          </div>
          <div>
            <span className="text-muted-foreground">Версия расчёта:</span>{" "}
            <strong>{order.calculationVersion}</strong>
          </div>
        </div>
        <div className="compact-grid mb-3 rounded-md border border-border bg-primary/5 p-3 text-sm">
          <Metric
            icon={<WalletCards />}
            label="Сумма"
            value={formatMoney(totals.grand)}
          />
          <Metric
            icon={<BadgePercent />}
            label="Скидка"
            value={`${order.automaticDiscount + order.specialDiscount}%`}
          />
          <Metric icon={<Scale />} label="Вес" value="12.5 кг" />
          <Metric icon={<Box />} label="Объём" value="0.08 м³" />
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Позиция</TableHead>
              <TableHead>Ед.</TableHead>
              <TableHead>Кол-во</TableHead>
              <TableHead>Поставщик</TableHead>
              <TableHead>Цена за ед.</TableHead>
              <TableHead>Сумма</TableHead>
              <TableHead>Скидка</TableHead>
              <TableHead>Со скидкой</TableHead>
              <TableHead>НДС</TableHead>
              <TableHead>С НДС</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {order.quotePositions.map((position, index) => (
              <TableRow key={position.id}>
                <TableCell className="font-semibold">
                  {position.product}
                </TableCell>
                <TableCell>{position.unit}</TableCell>
                <TableCell>{position.quantity}</TableCell>
                <TableCell>
                  <Select
                    value={
                      suppliers?.find((item) => item.name === position.supplier)
                        ?.id ?? "s-1"
                    }
                    onValueChange={async (supplierId) => {
                      await updateQuote.mutateAsync({
                        orderId: order.id,
                        payload: { supplierId },
                      });
                      toast.success("Поставщик изменён, КП пересчитано");
                    }}
                    disabled={index > 0}
                  >
                    <SelectTrigger className="min-w-44">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {suppliers?.map((supplier) => (
                        <SelectItem value={supplier.id} key={supplier.id}>
                          {supplier.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </TableCell>
                <TableCell>{formatMoney(position.price)}</TableCell>
                <TableCell>
                  {formatMoney(position.price * position.quantity)}
                </TableCell>
                <TableCell>{formatMoney(position.discount)}</TableCell>
                <TableCell>{formatMoney(position.subtotal)}</TableCell>
                <TableCell>
                  {formatMoney(
                    Math.round(position.subtotal * (order.vatRate / 100)),
                  )}
                </TableCell>
                <TableCell className="font-semibold">
                  {formatMoney(
                    position.subtotal +
                      Math.round(position.subtotal * (order.vatRate / 100)),
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
          <TableFooter>
            <TableRow>
              <TableCell colSpan={5} className="text-right">
                Итого по позициям:
              </TableCell>
              <TableCell>{formatMoney(totals.gross)}</TableCell>
              <TableCell>{formatMoney(totals.lineDiscount)}</TableCell>
              <TableCell>{formatMoney(totals.discounted)}</TableCell>
              <TableCell>{formatMoney(totals.vat)}</TableCell>
              <TableCell>{formatMoney(totals.grand)}</TableCell>
            </TableRow>
          </TableFooter>
        </Table>

        <div className="kp-totals">
          <div className="kp-total-row">
            <span>Сумма со скидками по позициям (без НДС)</span>
            <span>{formatMoney(totals.discounted)}</span>
          </div>
          <div className="kp-total-row">
            <span>
              Специальная скидка {order.specialDiscount}%{" "}
              <span className="manual-badge">изменено вручную</span>
            </span>
            <span className="text-green-600">
              −{formatMoney(totals.specialAmount)}
            </span>
          </div>
          <div className="kp-total-row">
            <span>База для НДС</span>
            <span>{formatMoney(totals.vatBase)}</span>
          </div>
          <div className="kp-total-row">
            <span>НДС {order.vatRate}%</span>
            <span>{formatMoney(totals.vat)}</span>
          </div>
          <div className="kp-total-row grand">
            <span>ИТОГО к оплате</span>
            <span>{formatMoney(totals.grand)}</span>
          </div>
        </div>

        <div className="mb-3 rounded-md border border-border bg-primary/5 p-3 text-sm">
          <div className="button-row">
            <span className="text-muted-foreground">Ставка НДС:</span>
            <strong>{order.vatRate}%</strong>
            <Button
              variant="outline"
              size="icon"
              onClick={() => openReason("vat")}
              aria-label="Изменить ставку НДС"
            >
              <PenLine />
            </Button>
          </div>
          <div className="discount-block mt-2">
            <div className="discount-auto">
              <span className="text-muted-foreground">
                Расчётная скидка:
              </span>
              <strong className="text-green-600">
                {order.automaticDiscount}% — Категория (ОП)
              </strong>
              <span className="auto-badge">рассчитано автоматически</span>
              <Button variant="link" onClick={() => navigate("/suppliers")}>
                <Info />
                правило
              </Button>
            </div>
            <div className="text-xs text-muted-foreground">
              Применено правило «Категория (ОП)». Объёмная скидка не сработала:
              сумма заказа ниже 300 000 ₽.
            </div>
            <div className="button-row mt-2">
              <span className="text-muted-foreground">
                Специальная скидка:
              </span>
              <strong>{order.specialDiscount}%</strong>
              <Button
                variant="outline"
                size="icon"
                onClick={() => openReason("discount")}
                aria-label="Изменить специальную скидку"
              >
                <PenLine />
              </Button>
              <span className="manual-badge">изменено вручную</span>
            </div>
          </div>
          <div className="mt-2 text-amber-600">
            Статус скидки:{" "}
            {order.discountApproval === "pending"
              ? "ожидает подтверждения Администратора"
              : order.discountApproval === "approved"
                ? "подтверждена"
                : "отклонена"}
          </div>
        </div>

        <div className="mb-2 font-semibold">Сводка по поставщикам</div>
        <div className="supplier-summary">
          {order.supplierTotals.map((supplier) => (
            <div className="ss-card" key={supplier.supplier}>
              <div className="ss-name">{supplier.supplier}</div>
              <div className="ss-row">
                <span className="text-muted-foreground">Сумма:</span>{" "}
                <strong>{formatMoney(supplier.amount)}</strong>
              </div>
              <div className="ss-row">
                <span className="text-muted-foreground">Вес:</span>{" "}
                {supplier.weight}
              </div>
              <div className="ss-row">
                <span className="text-muted-foreground">Объём:</span>{" "}
                {supplier.volume}
              </div>
            </div>
          ))}
        </div>
        <div className="button-row mt-3">
          <Tooltip>
            <TooltipTrigger asChild>
              {/* Декоративна до связывания контуров (вопрос №7 плана):
                  реального экспорта нет, файл не формируется. */}
              <span>
                <Button disabled>
                  <Download />
                  Скачать Excel
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              Появится после связывания заявки с расчётом Фазы 2
            </TooltipContent>
          </Tooltip>
          <Button
            variant="success"
            onClick={async () => {
              await orderAction.mutateAsync({
                orderId: order.id,
                action: "confirm-quote",
              });
              toast.success("КП подтверждён");
            }}
          >
            <Check />
            Подтвердить КП
          </Button>
          <Tooltip>
            <TooltipTrigger asChild>
              {/* Декоративна до связывания контуров: публикации в B24 из КП
                  нет, отправка не происходит. */}
              <span>
                <Button disabled variant="outline">
                  <Send />
                  Отправить в B24
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              Появится после связывания заявки с расчётом Фазы 2
            </TooltipContent>
          </Tooltip>
        </div>
      </div>

      <Dialog
        open={Boolean(reasonType)}
        onOpenChange={() => setReasonType(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <PenLine />
              {reasonType === "vat"
                ? "Изменение ставки НДС"
                : "Изменение специальной скидки"}
            </DialogTitle>
            <DialogDescription>
              Причина обязательна и попадёт в журнал изменений.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={saveReason}>
            <div className="grid gap-2">
              <Label htmlFor="reason-value">
                {reasonType === "vat" ? "Ставка НДС" : "Скидка"}, %
              </Label>
              <Input
                id="reason-value"
                type="number"
                {...reasonForm.register("value")}
              />
              <span className="field-error">
                {reasonForm.formState.errors.value?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="reason-text">Причина изменения</Label>
              <Input id="reason-text" {...reasonForm.register("reason")} />
              <span className="field-error">
                {reasonForm.formState.errors.reason?.message}
              </span>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setReasonType(null)}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={updateQuote.isPending}>
                Сохранить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Metric({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-primary [&_svg]:size-4">{icon}</span>
      <span className="text-muted-foreground">{label}:</span>
      <strong>{value}</strong>
    </div>
  );
}
