import { FileUp, Play, RotateCcw, Square } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { useAuth } from "@/features/auth/model";
import {
  useCancelItem,
  useItemDocuments,
  useItemRuns,
  useRunItem,
  useUploadItemDocument,
} from "@/shared/api/hooks";
import { formatBytes } from "@/shared/lib/utils";
import type { ItemDocument, OrderCard, OrderItem } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { LoadingState, Panel, StatusBadge } from "@/shared/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import { ItemPipeline } from "@/widgets/order-detail/item-pipeline";

/** Роли документов позиции: ТЗ и паспорт (docs/api/uploads §Multipart). */
const ROLES = [
  { role: "REQUIREMENT" as const, title: "Требования (ТЗ)" },
  { role: "FACT" as const, title: "Факт (паспорт)" },
];

/** Форматы, которые принимает `POST /uploads`. */
const ACCEPT = ".pdf,.xlsx,.docx";

/** Статусы позиции, из которых её можно запустить (checks/models.py). */
const RUNNABLE = ["DRAFT", "NEEDS_INPUT", "FAILED", "CANCELLED"];

/** Статусы незавершённой работы: их отменяет `cancel`. */
const CANCELLABLE = [
  "QUEUED",
  "INGESTING",
  "NEEDS_INPUT",
  "EXTRACTING",
  "VERIFYING_SOURCES",
  "NORMALIZING",
  "AWAITING_PARAMETER_REVIEW",
  "MATCHING",
];

const RUN_TONE: Record<string, "info" | "closed" | "danger" | "archived"> = {
  QUEUED: "info",
  RUNNING: "info",
  COMPLETED: "closed",
  FAILED: "danger",
  CANCELLED: "archived",
};

function formatMoment(value: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

function DocumentList({ documents }: { documents: ItemDocument[] }) {
  if (!documents.length) {
    return <div className="text-sm text-muted-foreground">Файлов нет</div>;
  }
  return (
    <ul className="flex flex-col gap-1.5">
      {documents.map((document) => (
        <li className="text-sm" key={document.documentId}>
          <span className="font-medium">{document.fileName ?? "—"}</span>
          <span className="ml-2 text-muted-foreground">
            {formatBytes(document.sizeBytes)} · {formatMoment(document.createdAt)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function UploadButton({
  itemId,
  role,
  disabled,
}: {
  itemId: string;
  role: "REQUIREMENT" | "FACT";
  disabled: boolean;
}) {
  const upload = useUploadItemDocument();
  const input = useRef<HTMLInputElement>(null);

  const send = async (file: File) => {
    try {
      await upload.mutateAsync({ itemId, file, role });
      toast.success(`Файл ${file.name} загружен`);
    } catch (cause) {
      // Причина отказа приходит из envelope'а: неподдерживаемый формат,
      // подмена расширения, превышение размера. Показывать её нужно как есть.
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось загрузить файл",
      );
    }
  };

  return (
    <>
      <input
        accept={ACCEPT}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          // Значение сбрасывается, иначе повторный выбор того же файла не
          // вызовет change и загрузка после ошибки не повторится.
          event.target.value = "";
          if (file) void send(file);
        }}
        ref={input}
        type="file"
      />
      <Button
        disabled={disabled || upload.isPending}
        onClick={() => input.current?.click()}
        size="sm"
        variant="outline"
      >
        <FileUp />
        {upload.isPending ? "Загрузка..." : "Загрузить"}
      </Button>
    </>
  );
}

function ItemRuns({ itemId }: { itemId: string }) {
  const { data, isLoading } = useItemRuns(itemId);

  if (isLoading) return <LoadingState label="Загрузка прогонов..." />;
  if (!data?.length) {
    return (
      <div className="p-1 text-sm text-muted-foreground">
        Проверка ещё не запускалась
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Прогон</TableHead>
          <TableHead>Статус</TableHead>
          <TableHead>Каталог</TableHead>
          <TableHead>Начат</TableHead>
          <TableHead>Завершён</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((run) => (
          <TableRow key={run.id}>
            <TableCell className="tabular-nums">№{run.runNo}</TableCell>
            <TableCell>
              <StatusBadge
                label={run.status}
                tone={RUN_TONE[run.status] ?? "info"}
              />
            </TableCell>
            <TableCell>{run.catalogVersion ?? "—"}</TableCell>
            <TableCell>{formatMoment(run.startedAt)}</TableCell>
            <TableCell>{formatMoment(run.finishedAt)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function ItemCard({ item, orderId }: { item: OrderItem; orderId: string }) {
  const { data: documents, isLoading } = useItemDocuments(item.id);
  const runItem = useRunItem();
  const cancelItem = useCancelItem();
  const { isAdminLike, user } = useAuth();
  const [showRuns, setShowRuns] = useState(false);

  // Право менять проверки — у менеджера и выше; viewer только читает.
  const canWrite = isAdminLike || user?.roles.includes("manager") === true;
  const runnable = canWrite && RUNNABLE.includes(item.status);
  const cancellable = canWrite && CANCELLABLE.includes(item.status);

  const start = async () => {
    try {
      await runItem.mutateAsync({ itemId: item.id, orderId });
      toast.success(`Позиция «${item.name}» отправлена на проверку`);
    } catch (cause) {
      // Предусловия запуска (нет ТЗ, нет паспорта, не подтверждён тип
      // оборудования) сервер отдаёт кодом и текстом — показываем текст.
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось запустить проверку",
      );
    }
  };

  const stop = async () => {
    await cancelItem.mutateAsync({ itemId: item.id, orderId });
    toast.success("Запрошена отмена проверки");
  };

  return (
    <Panel
      title={`Позиция ${item.positionNo}. ${item.name}`}
      actions={
        <div className="button-row">
          <StatusBadge
            label={item.statusLabel}
            tone={item.status === "FAILED" ? "danger" : "info"}
          />
          {runnable ? (
            <Button disabled={runItem.isPending} onClick={start} size="sm">
              {item.status === "DRAFT" ? <Play /> : <RotateCcw />}
              {item.status === "DRAFT" ? "Запустить проверку" : "Повторить"}
            </Button>
          ) : null}
          {cancellable ? (
            <Button
              disabled={cancelItem.isPending}
              onClick={stop}
              size="sm"
              variant="destructive"
            >
              <Square />
              Отменить
            </Button>
          ) : null}
        </div>
      }
    >
      <div className="p-5">
        {isLoading ? (
          <LoadingState label="Загрузка документов..." />
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            {ROLES.map(({ role, title }) => (
              <div key={role}>
                <div className="flex-between mb-2">
                  <span className="font-semibold">{title}</span>
                  <UploadButton
                    disabled={!canWrite}
                    itemId={item.id}
                    role={role}
                  />
                </div>
                <DocumentList
                  documents={
                    role === "REQUIREMENT"
                      ? (documents?.requirement ?? [])
                      : (documents?.fact ?? [])
                  }
                />
              </div>
            ))}
          </div>
        )}
        <ItemPipeline canWrite={canWrite} item={item} orderId={orderId} />

        <div className="mt-4">
          <Button
            onClick={() => setShowRuns((open) => !open)}
            size="sm"
            variant="link"
          >
            {showRuns ? "Скрыть историю проверок" : "История проверок"}
          </Button>
          {showRuns ? <ItemRuns itemId={item.id} /> : null}
        </div>
      </div>
    </Panel>
  );
}

export function OrderItems({ order }: { order: OrderCard }) {
  if (!order.items.length) {
    return (
      <div className="p-5 text-sm text-muted-foreground">
        В заявке нет позиций.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {order.items.map((item) => (
        <ItemCard item={item} key={item.id} orderId={order.id} />
      ))}
    </div>
  );
}
