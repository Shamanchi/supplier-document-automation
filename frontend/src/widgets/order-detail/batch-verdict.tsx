import { Check, Download, FileSpreadsheet, ListChecks } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  useBatchVerdict,
  useDecideBatchVerdict,
  useDownloadReportArtifact,
  useRequestBatchReport,
} from "@/shared/api/hooks";
import { saveBlob } from "@/shared/lib/utils";
import type { ReportArtifact, ReportStatus, VerdictValue } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { LoadingState, StatusBadge, type StatusTone } from "@/shared/ui/page";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import { Textarea } from "@/shared/ui/textarea";
import { BitrixPublishPanel } from "@/widgets/order-detail/bitrix";

const VERDICT_LABELS: Record<VerdictValue, string> = {
  PENDING: "Не подтверждён",
  COMPLIANT: "Соответствует",
  PARTIAL: "Частично",
  NON_COMPLIANT: "Не соответствует",
};

const DECISION_OPTIONS: Array<Exclude<VerdictValue, "PENDING">> = [
  "COMPLIANT",
  "PARTIAL",
  "NON_COMPLIANT",
];

function tone(value: VerdictValue): StatusTone {
  if (value === "COMPLIANT") return "success";
  if (value === "NON_COMPLIANT") return "danger";
  if (value === "PENDING") return "archived";
  return "warning";
}

/**
 * Вердикт заявки и сводный отчёт.
 *
 * Расчёт заявки — худший из действующих вердиктов позиций, поэтому кнопка
 * подтверждения появляется только когда по каждой позиции решение принято;
 * до этого backend отвечает `VERDICT_NOT_READY`, и показывать кнопку было бы
 * обманом. Сводный отчёт собирается синхронно и сразу приходит с файлом.
 */
export function BatchVerdictPanel({
  orderId,
  canWrite,
  tenderId,
}: {
  orderId: string;
  canWrite: boolean;
  tenderId?: string | null;
}) {
  const { data, isLoading } = useBatchVerdict(orderId);
  const decide = useDecideBatchVerdict();
  const requestReport = useRequestBatchReport();
  const download = useDownloadReportArtifact();
  const [verdict, setVerdict] =
    useState<Exclude<VerdictValue, "PENDING">>("COMPLIANT");
  const [comment, setComment] = useState("");
  const [report, setReport] = useState<ReportStatus | null>(null);

  if (isLoading) return <LoadingState label="Загрузка вердикта заявки..." />;
  if (!data) return null;

  const decided = data.managerVerdict !== "PENDING";
  const ready = data.computedVerdict !== "PENDING";

  const submit = async () => {
    try {
      await decide.mutateAsync({
        orderId,
        expectedVersion: data.version,
        verdict,
        comment: comment.trim() || undefined,
      });
      setComment("");
      toast.success("Вердикт заявки утверждён");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось утвердить вердикт",
      );
    }
  };

  const build = async () => {
    try {
      setReport(await requestReport.mutateAsync({ orderId }));
      toast.success("Сводный отчёт сформирован");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось собрать отчёт",
      );
    }
  };

  const load = async (artifact: ReportArtifact) => {
    try {
      const file = await download.mutateAsync({
        artifactId: artifact.id,
        fileName: artifact.fileName,
      });
      saveBlob(file.blob, file.fileName);
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось скачать отчёт",
      );
    }
  };

  return (
    <section className="panel grid gap-3 p-5">
      <div className="flex-between gap-3">
        <div className="flex items-center gap-2">
          <ListChecks className="size-4 text-primary" />
          <h4 className="text-sm font-semibold text-primary">Вердикт заявки</h4>
        </div>
        {canWrite && decided ? (
          <Button
            disabled={requestReport.isPending}
            onClick={build}
            size="sm"
            variant="outline"
          >
            <FileSpreadsheet />
            Сводный отчёт
          </Button>
        ) : null}
      </div>

      <div className="compact-grid">
        <div>
          <div className="text-xs text-muted-foreground">Расчётный</div>
          <StatusBadge
            label={VERDICT_LABELS[data.computedVerdict]}
            tone={tone(data.computedVerdict)}
          />
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Менеджер</div>
          <StatusBadge
            label={VERDICT_LABELS[data.managerVerdict]}
            tone={tone(data.managerVerdict)}
          />
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Позиций сведено</div>
          <div className="text-sm">
            {String((data.basis as Record<string, unknown> | null)?.items ?? "—")}
          </div>
        </div>
      </div>

      {!ready ? (
        <div className="text-sm text-muted-foreground">
          Вердикт заявки появится, когда по каждой позиции будет результат
          сравнения.
        </div>
      ) : null}

      {canWrite && ready && !decided ? (
        <div className="grid gap-3 md:grid-cols-[220px_1fr_auto]">
          <Select
            onValueChange={(value) =>
              setVerdict(value as Exclude<VerdictValue, "PENDING">)
            }
            value={verdict}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DECISION_OPTIONS.map((value) => (
                <SelectItem key={value} value={value}>
                  {VERDICT_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Textarea
            aria-label="Комментарий"
            className="min-h-9"
            onChange={(event) => setComment(event.target.value)}
            placeholder="Комментарий при отклонении от расчёта"
            value={comment}
          />
          <Button disabled={decide.isPending} onClick={submit}>
            <Check />
            Утвердить заявку
          </Button>
        </div>
      ) : null}

      {decided && data.comment ? (
        <div className="rounded-md border border-border p-3 text-sm">
          {data.comment}
        </div>
      ) : null}

      {report?.artifacts.length ? (
        <BitrixPublishPanel
          artifacts={report.artifacts}
          canWrite={canWrite}
          orderId={orderId}
          tenderId={tenderId}
        />
      ) : null}

      {report?.artifacts.length ? (
        <div className="data-list">
          {report.artifacts.map((artifact) => (
            <div className="data-list-row" key={artifact.id}>
              <div className="min-w-0">
                <div className="truncate font-medium">{artifact.fileName}</div>
                <div className="text-xs text-muted-foreground">
                  {artifact.format} ·{" "}
                  {artifact.sizeBytes
                    ? `${Math.round(artifact.sizeBytes / 1024)} КБ`
                    : "размер неизвестен"}
                </div>
              </div>
              <Button
                disabled={download.isPending}
                onClick={() => void load(artifact)}
                size="sm"
              >
                <Download />
                Скачать
              </Button>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
