import { BarChart3, FileText, Globe2, ListChecks, PenLine } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import type { OrderFile } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { StatusBadge } from "@/shared/ui/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

export function FileViewer({ file }: { file: OrderFile }) {
  const [open, setOpen] = useState(false);
  const rawColumns = file.rawRows?.[0] ? Object.keys(file.rawRows[0]) : [];

  return (
    <>
      <button
        type="button"
        className="file-item expandable w-full text-left"
        onClick={() => setOpen((value) => !value)}
      >
        <FileText className="file-icon" />
        <span className="file-name">{file.name}</span>
        <span className="file-version">{file.version}</span>
        <span className="file-date">{file.date}</span>
        <span className="file-progress">
          <span
            className="block h-full rounded"
            style={{
              width: `${file.progress}%`,
              background:
                file.progress === 100 ? "var(--ok)" : "var(--warn)",
            }}
          />
        </span>
        <span
          className="file-status"
          style={{
            color: file.progress === 100 ? "var(--ok)" : "var(--warn)",
          }}
        >
          {file.progress}%
        </span>
        <span className="file-expand">{open ? "свернуть" : "данные"}</span>
      </button>
      {open ? (
        <div className="file-data-viewer open !block">
          <Tabs defaultValue="raw">
            <TabsList>
              <TabsTrigger value="raw">
                <BarChart3 />
                Сырые данные
              </TabsTrigger>
              <TabsTrigger value="mapped">
                <ListChecks />
                Сопоставленные
              </TabsTrigger>
            </TabsList>
            <TabsContent value="raw">
              {file.status === "warning" ? (
                <div className="mb-2 text-sm text-amber-600">
                  Низкое качество распознавания — часть строк не извлечена.
                </div>
              ) : null}
              {file.rawRows?.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      {rawColumns.map((column) => (
                        <TableHead key={column}>{column}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {file.rawRows.map((row, index) => (
                      <TableRow key={`${file.id}-${index}`}>
                        {rawColumns.map((column) => (
                          <TableCell key={column}>{row[column]}</TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <div className="empty-state">Данные ещё обрабатываются</div>
              )}
              <Button
                variant="orange"
                size="sm"
                className="mt-2"
                onClick={() =>
                  toast.info(`Запущен углубленный поиск по ${file.name}`)
                }
              >
                <Globe2 />
                Углубленный поиск
              </Button>
            </TabsContent>
            <TabsContent value="mapped">
              <div className="data-list">
                {file.mappedRows?.map((row) => (
                  <div className="param-row" key={row.name}>
                    <span className="param-name">{row.name}</span>
                    <span>{row.value}</span>
                    {row.status === "missing" ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          toast.info(`Ручной ввод параметра ${row.name}`)
                        }
                      >
                        <PenLine />
                        Ввести
                      </Button>
                    ) : (
                      <StatusBadge
                        label="сопоставлено"
                        tone={
                          row.status === "success"
                            ? "success"
                            : row.status === "danger"
                              ? "danger"
                              : "warning"
                        }
                      />
                    )}
                  </div>
                ))}
              </div>
            </TabsContent>
          </Tabs>
        </div>
      ) : null}
    </>
  );
}
