import { PenLine, SquareKanban } from "lucide-react";

import type { OrderSummary } from "@/shared/model/types";
import { StatusBadge, type StatusTone } from "@/shared/ui/page";

const statusTone: Record<OrderSummary["status"], StatusTone> = {
  draft: "warning",
  active: "info",
  confirmation: "warning",
  closed: "closed",
  archived: "archived",
};

export function OrderStatusBadge({
  status,
  statusLabel,
  label,
}: Pick<OrderSummary, "status" | "statusLabel"> & { label?: string }) {
  return (
    <StatusBadge label={label ?? statusLabel} tone={statusTone[status]} />
  );
}

/**
 * Источник заявки. Отдельного поля в схеме Фазы 3 нет: пакет со ссылкой на
 * тендер пришёл из внешней системы, без неё — заведён руками.
 */
export function SourceTag({ tenderId }: Pick<OrderSummary, "tenderId">) {
  const external = Boolean(tenderId);
  const Icon = external ? SquareKanban : PenLine;
  return (
    <span className={`source-tag ${external ? "bitrix" : "manager"}`}>
      <Icon className="size-3.5" />
      {external ? `Тендер ${tenderId}` : "Вручную"}
    </span>
  );
}

export function Progress({
  value,
  tone,
}: {
  value: number;
  tone?: string;
}) {
  const color =
    tone ??
    (value === 100
      ? "var(--ok)"
      : value >= 70
        ? "var(--warn)"
        : "var(--brand-primary)");
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-20 overflow-hidden rounded"
        style={{ background: "var(--border-light)" }}
      >
        <div
          className="h-full rounded"
          style={{ width: `${value}%`, background: color }}
        />
      </div>
      <span className="text-[13px] font-medium tabular-nums">{value}%</span>
    </div>
  );
}
