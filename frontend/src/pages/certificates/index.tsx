import { FileSearch, FileText, Upload } from "lucide-react";
import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { useAuth } from "@/features/auth/model";
import { useCertificates, useUploadCertificate } from "@/shared/api/hooks";
import type { Certificate } from "@/shared/model/types";
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
import { ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from "@/shared/ui/page";

const uploadSchema = z.object({
  supplier: z.string().min(2, "Укажите поставщика"),
  product: z.string().min(2, "Укажите товар"),
  number: z.string().min(3, "Укажите номер документа"),
});

type UploadValues = z.infer<typeof uploadSchema>;
type Source = Certificate["parameters"][number];

export function CertificatesPage() {
  const { isAdminLike } = useAuth();
  const { data, isLoading, isError } = useCertificates();
  const upload = useUploadCertificate();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [source, setSource] = useState<Source | null>(null);
  const form = useForm<UploadValues>({
    resolver: zodResolver(uploadSchema),
    defaultValues: { supplier: "", product: "", number: "" },
  });

  const submitUpload = form.handleSubmit(async (values) => {
    await upload.mutateAsync(values);
    toast.success("Документ загружен");
    form.reset();
    setUploadOpen(false);
  });

  return (
    <>
      <PageHeader
        title="Техническая документация"
        subtitle="Паспорта, сертификаты и ТХ по изделиям"
        actions={
          isAdminLike ? (
            <Button onClick={() => setUploadOpen(true)}>
              <Upload />
              Загрузить документ
            </Button>
          ) : null
        }
      />
      {isLoading ? (
        <LoadingState />
      ) : isError || !data ? (
        <ErrorState />
      ) : (
        <div className="page-grid">
          {data.map((certificate) => (
            <Panel
              key={certificate.id}
              title={`${certificate.supplier} → ${certificate.product}`}
            >
              <div className="compact-grid p-5">
                <div>
                  <div className="mb-2 flex items-center gap-2 font-semibold">
                    <FileText className="size-4" />
                    Документ
                  </div>
                  <div className="rounded-md border border-border p-3">
                    <div className="data-list-row">
                      <span className="font-medium">{certificate.number}</span>
                      <StatusBadge
                        label={
                          certificate.status === "active"
                            ? "Активен"
                            : "Истёк"
                        }
                        tone={
                          certificate.status === "active"
                            ? "success"
                            : "danger"
                        }
                      />
                      <Button
                        variant="link"
                        onClick={() => toast.info("Документ открыт")}
                      >
                        <FileText />
                        Источник
                      </Button>
                    </div>
                    <div className="mt-2 text-xs text-muted-foreground">
                      Действует до {certificate.validUntil}
                    </div>
                  </div>
                </div>
                <div>
                  <div className="mb-2 flex items-center gap-2 font-semibold">
                    <FileSearch className="size-4" />
                    Технические характеристики
                  </div>
                  <div className="rounded-md border border-border p-3">
                    {certificate.parameters.map((parameter) => (
                      <div className="data-list-row" key={parameter.name}>
                        <span className="text-muted-foreground">
                          {parameter.name}
                        </span>
                        <Button
                          variant="link"
                          onClick={() => setSource(parameter)}
                        >
                          {parameter.value}
                          <FileText />
                        </Button>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </Panel>
          ))}
        </div>
      )}

      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <Upload />
              Загрузка документа
            </DialogTitle>
            <DialogDescription>
              Документ будет обработан и связан с товаром поставщика.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={submitUpload}>
            <div className="grid gap-2">
              <Label htmlFor="certificate-supplier">Поставщик</Label>
              <Input id="certificate-supplier" {...form.register("supplier")} />
              <span className="field-error">
                {form.formState.errors.supplier?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="certificate-product">Товар</Label>
              <Input id="certificate-product" {...form.register("product")} />
              <span className="field-error">
                {form.formState.errors.product?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="certificate-number">Номер</Label>
              <Input id="certificate-number" {...form.register("number")} />
              <span className="field-error">
                {form.formState.errors.number?.message}
              </span>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setUploadOpen(false)}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={upload.isPending}>
                <Upload />
                Загрузить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(source)} onOpenChange={() => setSource(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <FileText />
              Источник значения
            </DialogTitle>
          </DialogHeader>
          {source ? (
            <div className="data-list">
              <div className="data-list-row">
                <span className="text-muted-foreground">Параметр</span>
                <strong>{source.name}</strong>
              </div>
              <div className="data-list-row">
                <span className="text-muted-foreground">Значение</span>
                <strong>{source.value}</strong>
              </div>
              <div className="data-list-row">
                <span className="text-muted-foreground">Документ</span>
                <span>{source.document}</span>
              </div>
              <div className="data-list-row">
                <span className="text-muted-foreground">Страница</span>
                <span>{source.page}</span>
              </div>
              <blockquote className="rounded-md border-l-4 border-primary bg-muted p-3 text-sm">
                {source.quote}
              </blockquote>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
