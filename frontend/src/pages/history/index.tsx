import { FileText, RotateCcw, Search } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { useChecks } from "@/shared/api/hooks";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from "@/shared/ui/page";
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

export function HistoryPage() {
  const [draft, setDraft] = useState({ search: "", status: "all", date: "" });
  const [filters, setFilters] = useState(draft);
  const { data, isLoading, isError } = useChecks(filters);

  return (
    <>
      <PageHeader
        title="История проверок"
        subtitle="Все выполненные проверки ТХ"
      />
      <Panel>
        <div className="filter-row">
          <div className="filter-group">
            <label htmlFor="history-search">Поиск</label>
            <Input
              id="history-search"
              placeholder="Tender ID, клиент..."
              value={draft.search}
              onChange={(event) =>
                setDraft((value) => ({ ...value, search: event.target.value }))
              }
            />
          </div>
          <div className="filter-group">
            <label>Статус</label>
            <Select
              value={draft.status}
              onValueChange={(status) =>
                setDraft((value) => ({ ...value, status }))
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="success">Успешно</SelectItem>
                <SelectItem value="warning">Требует проверки</SelectItem>
                <SelectItem value="danger">Ошибка</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="filter-group">
            <label htmlFor="history-date">Дата</label>
            <Input
              id="history-date"
              type="date"
              value={draft.date}
              onChange={(event) =>
                setDraft((value) => ({ ...value, date: event.target.value }))
              }
            />
          </div>
          <Button size="sm" onClick={() => setFilters(draft)}>
            <Search />
            Найти
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              const empty = { search: "", status: "all", date: "" };
              setDraft(empty);
              setFilters(empty);
            }}
          >
            <RotateCcw />
            Сбросить
          </Button>
        </div>
        {isLoading ? (
          <LoadingState />
        ) : isError || !data ? (
          <ErrorState />
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Дата</TableHead>
                  <TableHead>Tender ID</TableHead>
                  <TableHead>Клиент</TableHead>
                  <TableHead>Тип оборудования</TableHead>
                  <TableHead>Инструмент</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Параметры</TableHead>
                  <TableHead>Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.date}</TableCell>
                    <TableCell>{item.tenderId}</TableCell>
                    <TableCell>{item.client}</TableCell>
                    <TableCell>{item.equipment}</TableCell>
                    <TableCell>
                      <span className="source-tag manager">{item.tool}</span>
                    </TableCell>
                    <TableCell>
                      <StatusBadge
                        label={item.statusLabel}
                        tone={item.status}
                      />
                    </TableCell>
                    <TableCell>{item.parameters}</TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => toast.info(`Открыт отчёт ${item.tenderId}`)}
                      >
                        <FileText />
                        Открыть
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="border-t border-border px-5 py-3 text-right text-sm text-muted-foreground">
              Показано {data.length} из 45 проверок
            </div>
          </>
        )}
      </Panel>
    </>
  );
}
