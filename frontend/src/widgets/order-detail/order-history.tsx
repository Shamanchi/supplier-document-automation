import { FileText } from "lucide-react";
import { toast } from "sonner";

import { formatMoney } from "@/shared/lib/utils";
import type { OrderDetail } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { StatusBadge } from "@/shared/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

export function OrderHistory({ order }: { order: OrderDetail }) {
  return (
    <div>
      <div className="mb-2 font-semibold">
        История расчётов по заявке №{order.number}
      </div>
      <Table className="history-table">
        <TableHeader>
          <TableRow>
            <TableHead>Версия</TableHead>
            <TableHead>Дата</TableHead>
            <TableHead>Автор</TableHead>
            <TableHead>Сумма</TableHead>
            <TableHead>Статус</TableHead>
            <TableHead>Итоговая</TableHead>
            <TableHead>Действия</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {order.versions.map((version) => (
            <TableRow
              className={version.final ? "version-final" : undefined}
              key={version.version}
            >
              <TableCell className="font-semibold">
                {version.version}
              </TableCell>
              <TableCell>{version.date}</TableCell>
              <TableCell>{version.author}</TableCell>
              <TableCell>{formatMoney(version.amount)}</TableCell>
              <TableCell>
                <StatusBadge
                  label={
                    version.status === "confirmed"
                      ? "Подтверждён"
                      : version.status === "draft"
                        ? "Черновик"
                        : "Отменён"
                  }
                  tone={
                    version.status === "confirmed"
                      ? "success"
                      : version.status === "draft"
                        ? "warning"
                        : "danger"
                  }
                />
              </TableCell>
              <TableCell>
                <span
                  style={{
                    color: version.final ? "var(--ok)" : "var(--text-muted)",
                  }}
                >
                  {version.final ? "Да" : "Нет"}
                </span>
              </TableCell>
              <TableCell>
                <Button
                  variant="link"
                  onClick={() =>
                    toast.info(`Открыт КП версии ${version.version}`)
                  }
                >
                  <FileText />
                  Открыть
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
