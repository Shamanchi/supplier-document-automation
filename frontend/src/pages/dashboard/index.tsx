import { ArrowRight, CheckCircle2, Clock3, TrendingUp } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { OrderStatusBadge, Progress } from "@/entities/order/ui";
import { useDashboard } from "@/shared/api/hooks";
import { Button } from "@/shared/ui/button";
import { ErrorState, LoadingState, PageHeader, Panel } from "@/shared/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

export function DashboardPage() {
  const navigate = useNavigate();
  const { data, isLoading, isError } = useDashboard();

  if (isLoading) return <LoadingState />;
  if (isError || !data) return <ErrorState />;

  return (
    <>
      <PageHeader
        title="Дашборд"
        subtitle="Сводка по заявкам и активности за последние 7 дней"
      />
      <div className="kpi-grid">
        {data.kpis.map((kpi) => (
          <div className="kpi-card" key={kpi.id}>
            <div className="kpi-label">{kpi.label}</div>
            <div className="kpi-value tabular-nums">{kpi.value}</div>
            <div
              className={kpi.tone === "success" ? "kpi-delta positive" : "kpi-delta"}
              style={kpi.tone === "warning" ? { color: "var(--warn)" } : undefined}
            >
              {kpi.id === "all" ? (
                <TrendingUp className="size-3.5" />
              ) : kpi.id === "done" ? (
                <CheckCircle2 className="size-3.5" />
              ) : (
                <Clock3 className="size-3.5" />
              )}
              {kpi.detail}
            </div>
          </div>
        ))}
      </div>
      <Panel
        title="Последние заявки"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate("/orders")}
          >
            Показать все
            <ArrowRight />
          </Button>
        }
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>№</TableHead>
              <TableHead>Клиент</TableHead>
              <TableHead>Статус</TableHead>
              <TableHead>Прогресс</TableHead>
              <TableHead>Дата</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.latestOrders.map((order) => (
              <TableRow key={order.id}>
                <TableCell className="font-semibold">{order.number}</TableCell>
                <TableCell>{order.client}</TableCell>
                <TableCell>
                  <OrderStatusBadge
                    status={order.status}
                    statusLabel={order.statusLabel}
                  />
                </TableCell>
                <TableCell>
                  <Progress value={order.progress} />
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {order.date}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Panel>
    </>
  );
}
