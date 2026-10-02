import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, Clock3, Loader2, LockKeyhole, XCircle } from "lucide-react";

import { cn } from "@/shared/lib/utils";

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex-between">
      <div>
        <h1 className="page-title">{title}</h1>
        <p className="page-subtitle">{subtitle}</p>
      </div>
      {actions ? <div className="button-row">{actions}</div> : null}
    </div>
  );
}

export function Panel({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("panel", className)}>
      {title || actions ? (
        <div className="panel-header">
          <span className="panel-title">{title}</span>
          {actions}
        </div>
      ) : null}
      <div className="panel-body">{children}</div>
    </section>
  );
}

export type StatusTone =
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "closed"
  | "archived";

const statusIcons = {
  success: CheckCircle2,
  warning: AlertCircle,
  danger: XCircle,
  info: Clock3,
  closed: LockKeyhole,
  archived: LockKeyhole,
};

export function StatusBadge({
  label,
  tone,
}: {
  label: string;
  tone: StatusTone;
}) {
  const Icon = statusIcons[tone];
  return (
    <span className={cn("status-badge", tone)}>
      <span className="status-icon">
        <Icon />
      </span>
      {label}
    </span>
  );
}

export function LoadingState({ label = "Загрузка данных..." }: { label?: string }) {
  return (
    <div className="loading-state">
      <Loader2 className="mr-2 size-4" />
      {label}
    </div>
  );
}

export function ErrorState({ label = "Не удалось загрузить данные" }: { label?: string }) {
  return (
    <div className="error-state">
      <AlertCircle className="mr-2 size-4 text-destructive" />
      {label}
    </div>
  );
}
