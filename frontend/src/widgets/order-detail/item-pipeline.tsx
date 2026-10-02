import {
  Activity,
  Check,
  Download,
  EyeOff,
  FileSpreadsheet,
  GitCompare,
  ListChecks,
  Pencil,
  Plus,
  RefreshCcw,
  SlidersHorizontal,
  Undo2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Progress } from "@/entities/order/ui";
import {
  useAddItemParameter,
  useCancelComparisonOverride,
  useComparisonCandidates,
  useConfirmItemParameters,
  useDecideItemVerdict,
  useDownloadReportArtifact,
  useExcludeParameter,
  useItemComparisons,
  useItemParameters,
  useItemVerdict,
  useOverrideComparison,
  usePipelineEvents,
  useRecalculateComparisons,
  useReport,
  useRequestItemReport,
  useRestoreParameter,
  useReviseParameter,
  useSelectComparisonCandidate,
} from "@/shared/api/hooks";
import type {
  ComparisonResult,
  ComparisonStatus,
  ItemParameter,
  OrderItem,
  ReportArtifact,
  VerdictValue,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import { Textarea } from "@/shared/ui/textarea";
import { saveBlob } from "@/shared/lib/utils";
import { LoadingState, StatusBadge, type StatusTone } from "@/shared/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

const ACTIVE_PIPELINE_STATUSES = [
  "QUEUED",
  "INGESTING",
  "EXTRACTING",
  "VERIFYING_SOURCES",
  "NORMALIZING",
  "MATCHING",
  "REPORTING",
];

const PARAMETER_VISIBLE_STATUSES = [
  "NEEDS_INPUT",
  "AWAITING_PARAMETER_REVIEW",
  "MATCHING",
  "REVIEW_REQUIRED",
  "READY_FOR_MANAGER_REVIEW",
  "VERDICT_APPROVED",
  "REPORTING",
  "COMPLETED",
];

const COMPARISON_VISIBLE_STATUSES = [
  "MATCHING",
  "REVIEW_REQUIRED",
  "READY_FOR_MANAGER_REVIEW",
  "VERDICT_APPROVED",
  "REPORTING",
  "COMPLETED",
];

const VERDICT_VISIBLE_STATUSES = [
  "REVIEW_REQUIRED",
  "READY_FOR_MANAGER_REVIEW",
  "VERDICT_APPROVED",
  "REPORTING",
  "COMPLETED",
];

const REPORT_VISIBLE_STATUSES = ["VERDICT_APPROVED", "REPORTING", "COMPLETED"];
const REVIEWABLE_STATUSES = ["READY_FOR_MANAGER_REVIEW", "REVIEW_REQUIRED"];
// NEEDS_INPUT — единственный вход в ручной ввод, когда из документа не
// подтвердилось ни одного значения. Первый же добавленный параметр переводит
// позицию в AWAITING_PARAMETER_REVIEW, и дальше идёт обычный путь.
const PARAMETER_EDITABLE_STATUSES = [
  "NEEDS_INPUT",
  "AWAITING_PARAMETER_REVIEW",
  "READY_FOR_MANAGER_REVIEW",
  "REVIEW_REQUIRED",
];

const STAGE_LABELS: Record<string, string> = {
  QUEUED: "В очереди",
  INGESTING: "Приём документов",
  EXTRACTING: "Извлечение",
  VERIFYING_SOURCES: "Проверка источников",
  NORMALIZING: "Нормализация",
  AWAITING_PARAMETER_REVIEW: "Проверка параметров",
  NEEDS_INPUT: "Нужен ручной ввод",
  MATCHING: "Сопоставление",
  REPORTING: "Отчёт",
};

const COMPARISON_LABELS: Record<ComparisonStatus, string> = {
  COMPLIANT: "Соответствует",
  NON_COMPLIANT: "Не соответствует",
  REVIEW_REQUIRED: "Нужна проверка",
  NOT_FOUND_IN_FACT: "Нет в факте",
  NOT_REQUIRED: "Не требуется",
};

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

const PARAMETER_REASON_OPTIONS = [
  { value: "OCR_ERROR", label: "Ошибка OCR" },
  { value: "PARSER_ERROR", label: "Ошибка парсера" },
  { value: "MANUAL_INPUT", label: "Ручной ввод" },
  { value: "NOT_APPLICABLE", label: "Не применимо" },
];

function hasStatus(status: string, values: string[]) {
  return values.includes(status);
}

function statusTone(status: ComparisonStatus | VerdictValue): StatusTone {
  if (status === "COMPLIANT") return "success";
  if (status === "NON_COMPLIANT") return "danger";
  if (status === "NOT_REQUIRED" || status === "PENDING") return "archived";
  return "warning";
}

function stageLabel(value: string | null | undefined) {
  if (!value) return "Ожидание";
  return STAGE_LABELS[value] ?? value;
}

function currentEvidence(parameter: ItemParameter) {
  // После ручной правки машинная цитата остаётся в списке как история —
  // подтверждает значение только та, у которой `current`.
  return parameter.evidence.find((item) => item.current) ?? null;
}

function clip(value: string, max = 90) {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function formatConfidence(value: string | null) {
  if (!value) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  const percent = numeric <= 1 ? numeric * 100 : numeric;
  return `${Math.round(percent)}%`;
}

function formatRawParameter(parameter: ItemParameter) {
  return [parameter.raw.valueText, parameter.raw.unitText]
    .filter(Boolean)
    .join(" ") || "—";
}

function formatTypedValue(value: Record<string, unknown> | null | undefined) {
  if (!value) return "—";
  const unit = typeof value.canonicalUnit === "string" ? value.canonicalUnit : "";
  if (value.kind === "RANGE") {
    return `${String(value.min ?? "—")}…${String(value.max ?? "—")} ${unit}`.trim();
  }
  if (value.kind === "SET" && Array.isArray(value.values)) {
    return `${value.values.map(String).join(", ")} ${unit}`.trim();
  }
  if (value.kind === "BOOLEAN") {
    return value.value ? "да" : "нет";
  }
  return `${String(value.value ?? "—")} ${unit}`.trim();
}

function operandText(value: unknown) {
  const operand = asRecord(value);
  if (!operand) return "—";
  return `${String(operand.value ?? "—")} ${String(operand.unit ?? "")}`.trim();
}

function explanationOf(comparison: ComparisonResult, key: "required" | "fact") {
  return operandText(asRecord(comparison.explanation)?.[key]);
}

function expressionOf(comparison: ComparisonResult) {
  const explanation = asRecord(comparison.explanation);
  return typeof explanation?.expression === "string"
    ? explanation.expression
    : "—";
}

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function PipelineProgressPanel({
  itemId,
  status,
}: {
  itemId: string;
  status: string;
}) {
  const active = hasStatus(status, ACTIVE_PIPELINE_STATUSES);
  const stream = usePipelineEvents(itemId, active);
  const latest = stream.latest;
  const progress =
    latest?.progress && latest.progress.total > 0
      ? Math.min(
          100,
          Math.round((latest.progress.current / latest.progress.total) * 100),
        )
      : active
        ? 10
        : 100;

  if (!active && !stream.events.length) return null;

  return (
    <div className="rounded-md border border-border bg-secondary/35 p-4">
      <div className="flex-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Activity className="size-4 text-primary" />
          <div className="min-w-0">
            <div className="text-sm font-semibold">
              {stageLabel(latest?.stage ?? status)}
            </div>
            <div className="text-xs text-muted-foreground">
              {stream.connected
                ? "Поток событий подключён"
                : stream.error
                  ? "Поток событий недоступен"
                  : "Ожидание событий"}
            </div>
          </div>
        </div>
        <Progress value={progress} />
      </div>
      {stream.error ? (
        <div className="mt-2 text-xs text-destructive">{stream.error}</div>
      ) : null}
      {stream.events.length ? (
        <div className="mt-3 grid gap-1 text-xs text-muted-foreground">
          {stream.events.slice(-3).map((event) => (
            <div key={event.id} className="flex items-center justify-between gap-3">
              <span>{stageLabel(event.stage)}</span>
              <span>{new Date(event.occurredAt).toLocaleTimeString("ru-RU")}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ParameterEditDialog({
  mode,
  parameter,
  itemId,
  orderId,
  onClose,
}: {
  mode: "add" | "edit" | null;
  parameter: ItemParameter | null;
  itemId: string;
  orderId: string;
  onClose: () => void;
}) {
  const add = useAddItemParameter();
  const revise = useReviseParameter();
  const [role, setRole] = useState<"REQUIREMENT" | "FACT">("REQUIREMENT");
  const [name, setName] = useState("");
  const [valueText, setValueText] = useState("");
  const [unitText, setUnitText] = useState("");
  const [reasonCode, setReasonCode] = useState("MANUAL_INPUT");
  const [comment, setComment] = useState("");

  useEffect(() => {
    if (mode === "add") {
      setRole("REQUIREMENT");
      setName("");
      setValueText("");
      setUnitText("");
      setReasonCode("MANUAL_INPUT");
      setComment("");
      return;
    }
    if (mode === "edit" && parameter) {
      setRole(parameter.role);
      setName(parameter.raw.name);
      setValueText(parameter.raw.valueText ?? "");
      setUnitText(parameter.raw.unitText ?? "");
      setReasonCode("OCR_ERROR");
      setComment("");
    }
  }, [mode, parameter]);

  const open = mode !== null;
  const pending = add.isPending || revise.isPending;
  const canSubmit = name.trim() && comment.trim() && reasonCode.trim();

  const submit = async () => {
    if (!canSubmit) return;
    try {
      if (mode === "add") {
        await add.mutateAsync({
          itemId,
          orderId,
          role,
          name: name.trim(),
          valueText: optionalText(valueText),
          unitText: optionalText(unitText),
          reasonCode,
          comment,
        });
        toast.success("Параметр добавлен");
      } else if (mode === "edit" && parameter) {
        await revise.mutateAsync({
          itemId,
          orderId,
          parameterId: parameter.id,
          expectedVersion: parameter.version,
          name: name.trim(),
          valueText: optionalText(valueText),
          unitText: optionalText(unitText),
          reasonCode,
          comment,
        });
        toast.success("Параметр обновлён");
      }
      onClose();
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось сохранить параметр",
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {mode === "add" ? <Plus /> : <Pencil />}
            {mode === "add" ? "Добавить параметр" : "Изменить параметр"}
          </DialogTitle>
          <DialogDescription>
            После сохранения сопоставления будут помечены устаревшими, а позиция
            вернётся к подтверждению параметров.
          </DialogDescription>
        </DialogHeader>
        <div className="dialog-form">
          <div className="grid gap-2">
            <label className="text-xs font-semibold text-muted-foreground">
              Роль
            </label>
            <Select
              disabled={mode === "edit"}
              onValueChange={(value) => setRole(value as "REQUIREMENT" | "FACT")}
              value={role}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="REQUIREMENT">ТЗ</SelectItem>
                <SelectItem value="FACT">Факт</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-2">
            <label className="text-xs font-semibold text-muted-foreground">
              Название
            </label>
            <Input
              aria-label="Название"
              onChange={(event) => setName(event.target.value)}
              value={name}
            />
          </div>
          <div className="grid gap-3 md:grid-cols-[1fr_140px]">
            <div className="grid gap-2">
              <label className="text-xs font-semibold text-muted-foreground">
                Значение
              </label>
              <Input
                aria-label="Значение"
                onChange={(event) => setValueText(event.target.value)}
                value={valueText}
              />
            </div>
            <div className="grid gap-2">
              <label className="text-xs font-semibold text-muted-foreground">
                Единица
              </label>
              <Input
                aria-label="Единица"
                onChange={(event) => setUnitText(event.target.value)}
                value={unitText}
              />
            </div>
          </div>
          <div className="grid gap-2">
            <label className="text-xs font-semibold text-muted-foreground">
              Причина
            </label>
            <Select onValueChange={setReasonCode} value={reasonCode}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PARAMETER_REASON_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Textarea
            aria-label="Комментарий"
            onChange={(event) => setComment(event.target.value)}
            placeholder="Комментарий обязателен"
            value={comment}
          />
        </div>
        <DialogFooter>
          <Button onClick={onClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button disabled={pending || !canSubmit} onClick={submit} type="button">
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ParameterStateDialog({
  action,
  parameter,
  itemId,
  orderId,
  onClose,
}: {
  action: "exclude" | "restore" | null;
  parameter: ItemParameter | null;
  itemId: string;
  orderId: string;
  onClose: () => void;
}) {
  const exclude = useExcludeParameter();
  const restore = useRestoreParameter();
  const [reasonCode, setReasonCode] = useState("NOT_APPLICABLE");
  const [comment, setComment] = useState("");

  useEffect(() => {
    if (!action) return;
    setReasonCode(action === "exclude" ? "NOT_APPLICABLE" : "MANUAL_INPUT");
    setComment("");
  }, [action]);

  const submit = async () => {
    if (!parameter || !action || !comment.trim()) return;
    const payload = {
      itemId,
      orderId,
      parameterId: parameter.id,
      expectedVersion: parameter.version,
      reasonCode,
      comment,
    };
    try {
      if (action === "exclude") {
        await exclude.mutateAsync(payload);
        toast.success("Параметр исключён");
      } else {
        await restore.mutateAsync(payload);
        toast.success("Параметр возвращён");
      }
      onClose();
    } catch (cause) {
      toast.error(
        cause instanceof Error
          ? cause.message
          : "Не удалось изменить состояние параметра",
      );
    }
  };

  return (
    <Dialog
      open={action !== null}
      onOpenChange={(nextOpen) => !nextOpen && onClose()}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {action === "restore" ? <Undo2 /> : <EyeOff />}
            {action === "restore" ? "Вернуть параметр" : "Исключить параметр"}
          </DialogTitle>
          <DialogDescription>
            Изменение будет записано в аудит и потребует повторного
            подтверждения параметров.
          </DialogDescription>
        </DialogHeader>
        <div className="dialog-form">
          <div className="rounded-md border border-border p-3 text-sm">
            {parameter?.raw.name ?? "—"} ·{" "}
            {parameter ? formatRawParameter(parameter) : "—"}
          </div>
          <Select onValueChange={setReasonCode} value={reasonCode}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PARAMETER_REASON_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Textarea
            aria-label="Комментарий"
            onChange={(event) => setComment(event.target.value)}
            placeholder="Комментарий обязателен"
            value={comment}
          />
        </div>
        <DialogFooter>
          <Button onClick={onClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={
              exclude.isPending || restore.isPending || !comment.trim()
            }
            onClick={submit}
            type="button"
          >
            {action === "restore" ? "Вернуть" : "Исключить"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ParameterReview({
  item,
  orderId,
  canWrite,
}: {
  item: OrderItem;
  orderId: string;
  canWrite: boolean;
}) {
  const visible = hasStatus(item.status, PARAMETER_VISIBLE_STATUSES);
  const editable = canWrite && hasStatus(item.status, PARAMETER_EDITABLE_STATUSES);
  const { data, isLoading } = useItemParameters(item.id, visible);
  const confirm = useConfirmItemParameters();
  const [editMode, setEditMode] = useState<"add" | "edit" | null>(null);
  const [selectedParameter, setSelectedParameter] =
    useState<ItemParameter | null>(null);
  const [stateAction, setStateAction] =
    useState<"exclude" | "restore" | null>(null);
  const counts = useMemo(() => {
    const items = data?.items ?? [];
    return {
      requirement: items.filter((parameter) => parameter.role === "REQUIREMENT").length,
      fact: items.filter((parameter) => parameter.role === "FACT").length,
      issues: items.filter((parameter) => parameter.issues.length > 0).length,
    };
  }, [data?.items]);

  if (!visible) return null;

  const approve = async () => {
    try {
      await confirm.mutateAsync({ itemId: item.id, orderId });
      toast.success("Параметры подтверждены, сопоставление запущено");
    } catch (cause) {
      toast.error(
        cause instanceof Error
          ? cause.message
          : "Не удалось подтвердить параметры",
      );
    }
  };

  return (
    <section className="grid gap-3 border-t border-border pt-4">
      <div className="flex-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-primary">
            Извлечённые параметры
          </h4>
          <div className="mt-1 text-xs text-muted-foreground">
            ТЗ: {counts.requirement} · Факт: {counts.fact} · Замечания:{" "}
            {counts.issues}
          </div>
          {item.status === "NEEDS_INPUT" ? (
            <div className="mt-1 text-xs text-destructive">
              В паспорте не распозналось ни одного значения. Добавьте
              характеристики вручную или загрузите документ лучшего качества.
            </div>
          ) : null}
        </div>
        {editable ? (
          <div className="button-row">
            <Button
              onClick={() => {
                setSelectedParameter(null);
                setEditMode("add");
              }}
              size="sm"
              variant="outline"
            >
              <Plus />
              Добавить
            </Button>
            {item.status === "AWAITING_PARAMETER_REVIEW" ? (
              <Button disabled={confirm.isPending} onClick={approve} size="sm">
                <Check />
                Подтвердить параметры
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {isLoading ? (
        <LoadingState label="Загрузка параметров..." />
      ) : data?.items.length ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Роль</TableHead>
              <TableHead>Параметр</TableHead>
              <TableHead>Исходное значение</TableHead>
              <TableHead>Нормализация</TableHead>
              <TableHead>Источник</TableHead>
              <TableHead>Уверенность</TableHead>
              <TableHead>Действия</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((parameter) => (
              <TableRow
                className={parameter.excluded ? "opacity-60" : undefined}
                key={parameter.id}
              >
                <TableCell>
                  <div>{parameter.role === "REQUIREMENT" ? "ТЗ" : "Факт"}</div>
                  {parameter.excluded ? (
                    <span className="manual-badge">исключён</span>
                  ) : parameter.manuallyEdited ? (
                    <span className="manual-badge">ручной</span>
                  ) : null}
                </TableCell>
                <TableCell>
                  <div className="font-medium">{parameter.raw.name}</div>
                  {parameter.issues.length ? (
                    <div className="mt-1 text-xs text-destructive">
                      {parameter.issues
                        .map((issue) => issue.description || issue.issueType)
                        .join("; ")}
                    </div>
                  ) : null}
                </TableCell>
                <TableCell>{formatRawParameter(parameter)}</TableCell>
                <TableCell>
                  <div>{parameter.normalized?.canonicalName ?? "—"}</div>
                  <div className="text-xs text-muted-foreground">
                    {formatTypedValue(parameter.normalized?.typedValue)}
                  </div>
                </TableCell>
                <TableCell>
                  {(() => {
                    const evidence = currentEvidence(parameter);
                    if (!evidence) return "—";
                    return (
                      <span title={evidence.quote}>{clip(evidence.quote)}</span>
                    );
                  })()}
                </TableCell>
                <TableCell>{formatConfidence(parameter.confidence)}</TableCell>
                <TableCell>
                  {editable ? (
                    <div className="button-row">
                      <Button
                        onClick={() => {
                          setSelectedParameter(parameter);
                          setEditMode("edit");
                        }}
                        size="sm"
                        variant="outline"
                      >
                        <Pencil />
                        Изменить
                      </Button>
                      <Button
                        onClick={() => {
                          setSelectedParameter(parameter);
                          setStateAction(
                            parameter.excluded ? "restore" : "exclude",
                          );
                        }}
                        size="sm"
                        variant="outline"
                      >
                        {parameter.excluded ? <Undo2 /> : <EyeOff />}
                        {parameter.excluded ? "Вернуть" : "Исключить"}
                      </Button>
                    </div>
                  ) : (
                    <span className="text-sm text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
          Подтверждённых source verifier параметров пока нет.
        </div>
      )}
      <ParameterEditDialog
        itemId={item.id}
        mode={editMode}
        onClose={() => {
          setEditMode(null);
          setSelectedParameter(null);
        }}
        orderId={orderId}
        parameter={selectedParameter}
      />
      <ParameterStateDialog
        action={stateAction}
        itemId={item.id}
        onClose={() => {
          setStateAction(null);
          setSelectedParameter(null);
        }}
        orderId={orderId}
        parameter={selectedParameter}
      />
    </section>
  );
}

function OverrideDialog({
  comparison,
  itemId,
  orderId,
  onClose,
}: {
  comparison: ComparisonResult | null;
  itemId: string;
  orderId: string;
  onClose: () => void;
}) {
  const override = useOverrideComparison();
  const [status, setStatus] = useState<ComparisonStatus>("REVIEW_REQUIRED");
  const [comment, setComment] = useState("");

  useEffect(() => {
    if (!comparison) return;
    setStatus(comparison.effectiveStatus);
    setComment("");
  }, [comparison]);

  const submit = async () => {
    if (!comparison) return;
    try {
      await override.mutateAsync({
        itemId,
        orderId,
        comparisonId: comparison.id,
        expectedVersion: comparison.version,
        status,
        reasonCode: "MANAGER_DECISION",
        comment,
      });
      toast.success("Ручной статус сохранён");
      onClose();
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось сохранить статус",
      );
    }
  };

  return (
    <Dialog open={Boolean(comparison)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            <SlidersHorizontal />
            Ручной статус
          </DialogTitle>
          <DialogDescription>
            Расчётный статус останется в истории, а итог строки возьмётся из
            решения менеджера.
          </DialogDescription>
        </DialogHeader>
        <div className="dialog-form">
          <Select
            onValueChange={(value) => setStatus(value as ComparisonStatus)}
            value={status}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(COMPARISON_LABELS).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Textarea
            aria-label="Комментарий"
            onChange={(event) => setComment(event.target.value)}
            placeholder="Комментарий обязателен"
            value={comment}
          />
        </div>
        <DialogFooter>
          <Button onClick={onClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={override.isPending || !comment.trim()}
            onClick={submit}
            type="button"
          >
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CandidateDialog({
  comparison,
  itemId,
  orderId,
  onClose,
}: {
  comparison: ComparisonResult | null;
  itemId: string;
  orderId: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useComparisonCandidates(
    comparison?.id ?? null,
    Boolean(comparison),
  );
  const select = useSelectComparisonCandidate();
  const [comment, setComment] = useState("");

  useEffect(() => {
    if (comparison) setComment("");
  }, [comparison]);

  const choose = async (factParameterId: string) => {
    if (!comparison) return;
    try {
      await select.mutateAsync({
        itemId,
        orderId,
        comparisonId: comparison.id,
        expectedVersion: comparison.version,
        factParameterId,
        comment,
      });
      toast.success("Кандидат выбран, строка пересчитана");
      onClose();
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось выбрать кандидата",
      );
    }
  };

  return (
    <Dialog open={Boolean(comparison)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            <GitCompare />
            Кандидаты факта
          </DialogTitle>
          <DialogDescription>
            Ручной выбор доступен только для подходящих кандидатов той же
            размерности.
          </DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <LoadingState label="Загрузка кандидатов..." />
        ) : data?.candidates.length ? (
          <div className="grid gap-3">
            <Textarea
              onChange={(event) => setComment(event.target.value)}
              placeholder="Комментарий к ручному выбору"
              value={comment}
            />
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Ранг</TableHead>
                  <TableHead>Метод</TableHead>
                  <TableHead>Score</TableHead>
                  <TableHead>Факт</TableHead>
                  <TableHead>Действие</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.candidates.map((candidate) => (
                  <TableRow key={`${candidate.rank}-${candidate.factParameterId}`}>
                    <TableCell>{candidate.rank ?? "—"}</TableCell>
                    <TableCell>{candidate.method}</TableCell>
                    <TableCell>{candidate.score ?? "—"}</TableCell>
                    <TableCell>
                      <div className="font-mono text-xs">
                        {candidate.factParameterId ?? "—"}
                      </div>
                      {!candidate.eligible ? (
                        <div className="mt-1 text-xs text-muted-foreground">
                          {candidate.rejectionReasons.join("; ") || "Недоступен"}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <Button
                        disabled={
                          !candidate.eligible ||
                          !candidate.factParameterId ||
                          select.isPending
                        }
                        onClick={() =>
                          candidate.factParameterId
                            ? void choose(candidate.factParameterId)
                            : undefined
                        }
                        size="sm"
                        variant="outline"
                      >
                        <Check />
                        Выбрать
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
            Кандидатов для этой строки нет.
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ComparisonsReview({
  item,
  orderId,
  canWrite,
}: {
  item: OrderItem;
  orderId: string;
  canWrite: boolean;
}) {
  const visible = hasStatus(item.status, COMPARISON_VISIBLE_STATUSES);
  const editable = canWrite && hasStatus(item.status, REVIEWABLE_STATUSES);
  const { data, isLoading } = useItemComparisons(item.id, visible);
  const recalculate = useRecalculateComparisons();
  const cancelOverride = useCancelComparisonOverride();
  const [overrideRow, setOverrideRow] = useState<ComparisonResult | null>(null);
  const [candidateRow, setCandidateRow] = useState<ComparisonResult | null>(null);

  const counts = useMemo(() => {
    const base: Record<ComparisonStatus, number> = {
      COMPLIANT: 0,
      NON_COMPLIANT: 0,
      REVIEW_REQUIRED: 0,
      NOT_FOUND_IN_FACT: 0,
      NOT_REQUIRED: 0,
    };
    for (const row of data ?? []) base[row.effectiveStatus] += 1;
    return base;
  }, [data]);

  if (!visible) return null;

  const recalc = async () => {
    try {
      await recalculate.mutateAsync({ itemId: item.id, orderId });
      toast.success("Сопоставления пересчитаны");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось пересчитать строки",
      );
    }
  };

  const removeOverride = async (comparison: ComparisonResult) => {
    try {
      await cancelOverride.mutateAsync({
        itemId: item.id,
        orderId,
        comparisonId: comparison.id,
        expectedVersion: comparison.version,
      });
      toast.success("Ручной статус отменён");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось отменить override",
      );
    }
  };

  return (
    <section className="grid gap-3 border-t border-border pt-4">
      <div className="flex-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-primary">Сопоставление</h4>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
            {Object.entries(counts).map(([status, value]) => (
              <span key={status}>
                {COMPARISON_LABELS[status as ComparisonStatus]}: {value}
              </span>
            ))}
          </div>
        </div>
        {editable ? (
          <Button
            disabled={recalculate.isPending}
            onClick={recalc}
            size="sm"
            variant="outline"
          >
            <RefreshCcw />
            Пересчитать
          </Button>
        ) : null}
      </div>
      {isLoading ? (
        <LoadingState label="Загрузка сравнений..." />
      ) : data?.length ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Параметр</TableHead>
              <TableHead>Требование</TableHead>
              <TableHead>Факт</TableHead>
              <TableHead>Правило</TableHead>
              <TableHead>Статус</TableHead>
              <TableHead>Действия</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.map((comparison) => (
              <TableRow key={comparison.id}>
                <TableCell>
                  <div className="font-medium">
                    {comparison.canonicalParameter?.name ?? "—"}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {comparison.rowKind}
                  </div>
                </TableCell>
                <TableCell>{explanationOf(comparison, "required")}</TableCell>
                <TableCell>{explanationOf(comparison, "fact")}</TableCell>
                <TableCell>
                  <div>{comparison.operator ?? "—"}</div>
                  <div className="text-xs text-muted-foreground">
                    {expressionOf(comparison)}
                  </div>
                </TableCell>
                <TableCell>
                  <div className="grid gap-1">
                    <StatusBadge
                      label={COMPARISON_LABELS[comparison.effectiveStatus]}
                      tone={statusTone(comparison.effectiveStatus)}
                    />
                    {comparison.overridden ? (
                      <span className="manual-badge">
                        расчёт: {COMPARISON_LABELS[comparison.computedStatus]}
                      </span>
                    ) : null}
                  </div>
                </TableCell>
                <TableCell>
                  {editable ? (
                    <div className="button-row">
                      <Button
                        onClick={() => setCandidateRow(comparison)}
                        size="sm"
                        variant="outline"
                      >
                        <GitCompare />
                        Кандидаты
                      </Button>
                      <Button
                        onClick={() => setOverrideRow(comparison)}
                        size="sm"
                        variant="outline"
                      >
                        <SlidersHorizontal />
                        Статус
                      </Button>
                      {comparison.overridden ? (
                        <Button
                          disabled={cancelOverride.isPending}
                          onClick={() => void removeOverride(comparison)}
                          size="sm"
                          variant="outline"
                        >
                          <Undo2 />
                          Отменить
                        </Button>
                      ) : null}
                    </div>
                  ) : (
                    <span className="text-sm text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
          Сравнения появятся после подтверждения параметров и этапа matching.
        </div>
      )}
      <OverrideDialog
        comparison={overrideRow}
        itemId={item.id}
        onClose={() => setOverrideRow(null)}
        orderId={orderId}
      />
      <CandidateDialog
        comparison={candidateRow}
        itemId={item.id}
        onClose={() => setCandidateRow(null)}
        orderId={orderId}
      />
    </section>
  );
}

function VerdictPanel({
  item,
  orderId,
  canWrite,
}: {
  item: OrderItem;
  orderId: string;
  canWrite: boolean;
}) {
  const visible = hasStatus(item.status, VERDICT_VISIBLE_STATUSES);
  const editable = canWrite && hasStatus(item.status, REVIEWABLE_STATUSES);
  const { data, isLoading } = useItemVerdict(item.id, visible);
  const decide = useDecideItemVerdict();
  const [verdict, setVerdict] =
    useState<Exclude<VerdictValue, "PENDING">>("PARTIAL");
  const [comment, setComment] = useState("");

  useEffect(() => {
    if (!data) return;
    setVerdict(
      data.managerVerdict !== "PENDING"
        ? data.managerVerdict
        : data.computedVerdict === "PENDING"
          ? "PARTIAL"
          : data.computedVerdict,
    );
    setComment(data.comment ?? "");
  }, [data]);

  if (!visible) return null;

  const submit = async () => {
    if (!data) return;
    try {
      await decide.mutateAsync({
        itemId: item.id,
        orderId,
        expectedVersion: data.version,
        verdict,
        comment,
      });
      toast.success("Вердикт позиции утверждён");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось утвердить вердикт",
      );
    }
  };

  return (
    <section className="grid gap-3 border-t border-border pt-4">
      <div className="flex items-center gap-2">
        <ListChecks className="size-4 text-primary" />
        <h4 className="text-sm font-semibold text-primary">Вердикт</h4>
      </div>
      {isLoading ? (
        <LoadingState label="Загрузка вердикта..." />
      ) : data ? (
        <div className="grid gap-3">
          <div className="compact-grid">
            <div>
              <div className="text-xs text-muted-foreground">Расчётный</div>
              <StatusBadge
                label={VERDICT_LABELS[data.computedVerdict]}
                tone={statusTone(data.computedVerdict)}
              />
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Менеджер</div>
              <StatusBadge
                label={VERDICT_LABELS[data.managerVerdict]}
                tone={statusTone(data.managerVerdict)}
              />
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Утверждён</div>
              <div className="text-sm">
                {data.approvedAt
                  ? new Date(data.approvedAt).toLocaleString("ru-RU")
                  : "—"}
              </div>
            </div>
          </div>
          {editable ? (
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
                className="min-h-9"
                onChange={(event) => setComment(event.target.value)}
                placeholder="Комментарий при отклонении от расчёта"
                value={comment}
              />
              <Button disabled={decide.isPending} onClick={submit}>
                <Check />
                Утвердить
              </Button>
            </div>
          ) : data.comment ? (
            <div className="rounded-md border border-border p-3 text-sm">
              {data.comment}
            </div>
          ) : null}
        </div>
      ) : (
        <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
          Вердикт появится после сопоставления характеристик.
        </div>
      )}
    </section>
  );
}

function ReportPanel({
  item,
  orderId,
  canWrite,
}: {
  item: OrderItem;
  orderId: string;
  canWrite: boolean;
}) {
  const visible = hasStatus(item.status, REPORT_VISIBLE_STATUSES);
  const requestReport = useRequestItemReport();
  const download = useDownloadReportArtifact();
  const [reportId, setReportId] = useState<string | null>(null);
  const { data } = useReport(reportId, Boolean(reportId));

  if (!visible) return null;

  const request = async () => {
    try {
      const result = await requestReport.mutateAsync({ itemId: item.id, orderId });
      setReportId(result.reportId);
      toast.success("Формирование XLSX-отчёта запущено");
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось запросить отчёт",
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
    <section className="grid gap-3 border-t border-border pt-4">
      <div className="flex-between gap-3">
        <div className="flex items-center gap-2">
          <FileSpreadsheet className="size-4 text-primary" />
          <h4 className="text-sm font-semibold text-primary">Отчёт</h4>
        </div>
        {canWrite ? (
          <Button
            disabled={requestReport.isPending || item.status === "REPORTING"}
            onClick={request}
            size="sm"
            variant="outline"
          >
            <FileSpreadsheet />
            Сформировать XLSX
          </Button>
        ) : null}
      </div>
      {data ? (
        <div className="grid gap-3">
          <div className="button-row">
            <StatusBadge
              label={data.status}
              tone={
                data.status === "FAILED"
                  ? "danger"
                  : data.status === "COMPLETED"
                    ? "success"
                    : "info"
              }
            />
            {data.checksum ? (
              <span className="font-mono text-xs text-muted-foreground">
                {data.checksum.slice(0, 12)}
              </span>
            ) : null}
          </div>
          {data.artifacts.length ? (
            <div className="data-list">
              {data.artifacts.map((artifact) => (
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
          ) : (
            <div className="text-sm text-muted-foreground">
              Файл появится после завершения генерации.
            </div>
          )}
        </div>
      ) : (
        <div className="text-sm text-muted-foreground">
          XLSX формируется после утверждения вердикта позиции.
        </div>
      )}
    </section>
  );
}

export function ItemPipeline({
  item,
  orderId,
  canWrite,
}: {
  item: OrderItem;
  orderId: string;
  canWrite: boolean;
}) {
  const hasWorkflow =
    hasStatus(item.status, ACTIVE_PIPELINE_STATUSES) ||
    hasStatus(item.status, PARAMETER_VISIBLE_STATUSES) ||
    hasStatus(item.status, COMPARISON_VISIBLE_STATUSES) ||
    hasStatus(item.status, VERDICT_VISIBLE_STATUSES) ||
    hasStatus(item.status, REPORT_VISIBLE_STATUSES);

  if (!hasWorkflow) return null;

  return (
    <div className="mt-5 grid gap-4">
      <PipelineProgressPanel itemId={item.id} status={item.status} />
      <ParameterReview canWrite={canWrite} item={item} orderId={orderId} />
      <ComparisonsReview canWrite={canWrite} item={item} orderId={orderId} />
      <VerdictPanel canWrite={canWrite} item={item} orderId={orderId} />
      <ReportPanel canWrite={canWrite} item={item} orderId={orderId} />
    </div>
  );
}
