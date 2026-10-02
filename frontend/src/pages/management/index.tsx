import {
  Check,
  Info,
  ListChecks,
  Plus,
  Ruler,
  Save,
  Settings,
  Waypoints,
  X,
} from "lucide-react";
import { useEffect } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import {
  useAdminData,
  useDictionary,
  useSynonymDecision,
  useUpdateSettings,
} from "@/shared/api/hooks";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Label } from "@/shared/ui/label";
import { ErrorState, LoadingState, PageHeader, Panel } from "@/shared/ui/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/shared/ui/tooltip";

const settingsSchema = z
  .object({
    vatRate: z.coerce.number().min(0).max(100),
    confidenceHigh: z.coerce.number().min(1).max(100),
    confidenceMedium: z.coerce.number().min(1).max(99),
  })
  .refine((value) => value.confidenceMedium < value.confidenceHigh, {
    message: "Средний порог должен быть ниже высокого",
    path: ["confidenceMedium"],
  });

export function ManagementPage() {
  const admin = useAdminData();
  const dictionary = useDictionary();
  const updateSettings = useUpdateSettings();
  const synonymDecision = useSynonymDecision();
  const form = useForm<z.infer<typeof settingsSchema>>({
    resolver: zodResolver(settingsSchema),
    defaultValues: {
      vatRate: 22,
      confidenceHigh: 90,
      confidenceMedium: 70,
    },
  });

  useEffect(() => {
    if (admin.data?.settings) form.reset(admin.data.settings);
  }, [admin.data?.settings, form]);

  if (admin.isLoading || dictionary.isLoading) return <LoadingState />;
  if (admin.isError || dictionary.isError || !admin.data || !dictionary.data) {
    return <ErrorState />;
  }

  const saveSettings = form.handleSubmit(async (values) => {
    await updateSettings.mutateAsync(values);
    toast.success("Настройки сохранены");
  });

  return (
    <>
      <PageHeader
        title="Управление"
        subtitle="Настройки системы (Администратор и Сисадмин)"
      />
      <Tabs defaultValue="settings">
        <TabsList>
          <TabsTrigger value="settings">
            <Settings />
            Настройки
          </TabsTrigger>
          <TabsTrigger value="synonyms">
            <ListChecks />
            Синонимы
          </TabsTrigger>
          <TabsTrigger value="units">
            <Ruler />
            Единицы
          </TabsTrigger>
          <TabsTrigger value="materials">
            <Waypoints />
            Материалы
          </TabsTrigger>
          <TabsTrigger value="templates">
            <ListChecks />
            Шаблоны
          </TabsTrigger>
        </TabsList>
        <TabsContent value="settings">
          <Panel title="Настройки системы">
            <form className="grid gap-5 p-5" onSubmit={saveSettings}>
              <div className="rounded-md border border-border bg-muted/40 p-4">
                <Label htmlFor="vat-rate">Ставка НДС по умолчанию</Label>
                <div className="mt-2 flex items-center gap-3">
                  <Input
                    id="vat-rate"
                    type="number"
                    className="w-20"
                    {...form.register("vatRate")}
                  />
                  <span>%</span>
                  <span className="text-sm text-muted-foreground">
                    Применяется ко всем новым КП
                  </span>
                </div>
              </div>
              <div>
                <div className="mb-2 font-semibold">Пороги уверенности</div>
                <div className="compact-grid">
                  <ThresholdField
                    label="Высокий"
                    hint="Рекомендуется добавить в базу синонимов"
                    color="var(--ok)"
                    input={
                      <Input
                        type="number"
                        className="w-20"
                        {...form.register("confidenceHigh")}
                      />
                    }
                  />
                  <ThresholdField
                    label="Средний"
                    hint="Рекомендуется проверить вручную"
                    color="var(--warn)"
                    input={
                      <Input
                        type="number"
                        className="w-20"
                        {...form.register("confidenceMedium")}
                      />
                    }
                  />
                  <ThresholdField
                    label="Низкий"
                    hint="Новый синоним, требуется подтверждение"
                    color="var(--danger)"
                    input={
                      <span className="font-semibold">
                        &lt; {form.watch("confidenceMedium")}%
                      </span>
                    }
                  />
                </div>
                <span className="field-error">
                  {form.formState.errors.confidenceMedium?.message}
                </span>
              </div>
              <Button type="submit" className="w-fit" disabled={updateSettings.isPending}>
                <Save />
                Сохранить
              </Button>
            </form>
          </Panel>
        </TabsContent>
        <TabsContent value="synonyms">
          <Panel title="Предложенные синонимы">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Синоним</TableHead>
                  <TableHead>Каноническое название</TableHead>
                  <TableHead>Предложил</TableHead>
                  <TableHead>Сходство</TableHead>
                  <TableHead>Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {admin.data.synonyms.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.synonym}</TableCell>
                    <TableCell>{item.canonical}</TableCell>
                    <TableCell>{item.author}</TableCell>
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <strong
                            style={{
                              color:
                                item.similarity >= 90
                                  ? "var(--ok)"
                                  : item.similarity >= 70
                                    ? "var(--warn)"
                                    : "var(--danger)",
                            }}
                          >
                            {item.similarity}%
                          </strong>
                        </TooltipTrigger>
                        <TooltipContent>
                          {item.similarity >= 90
                            ? "Высокое сходство"
                            : item.similarity >= 70
                              ? "Требуется ручная проверка"
                              : "Новый синоним"}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      {item.status === "pending" ? (
                        <div className="button-row">
                          <Button
                            size="sm"
                            onClick={async () => {
                              await synonymDecision.mutateAsync({
                                synonymId: item.id,
                                status: "accepted",
                              });
                              toast.success("Синоним принят");
                            }}
                          >
                            <Check />
                            Принять
                          </Button>
                          <Button
                            variant="destructive"
                            size="sm"
                            onClick={async () => {
                              await synonymDecision.mutateAsync({
                                synonymId: item.id,
                                status: "rejected",
                              });
                              toast.success("Синоним отклонён");
                            }}
                          >
                            <X />
                            Отклонить
                          </Button>
                        </div>
                      ) : (
                        <span className="text-sm text-muted-foreground">
                          {item.status === "accepted" ? "Принят" : "Отклонён"}
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Panel>
        </TabsContent>
        <TabsContent value="units">
          <Panel
            title="Единицы измерения (конвертер)"
            actions={
              <Button size="sm" onClick={() => toast.info("Форма добавления открыта")}>
                <Plus />
                Добавить
              </Button>
            }
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Единица</TableHead>
                  <TableHead>Код</TableHead>
                  <TableHead>Размерность</TableHead>
                  <TableHead>Перевод</TableHead>
                  <TableHead>Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {dictionary.data.units.map((unit) => (
                  <TableRow key={unit.id}>
                    <TableCell className="font-semibold">{unit.symbol}</TableCell>
                    <TableCell>{unit.code}</TableCell>
                    <TableCell>{unit.dimension}</TableCell>
                    <TableCell className="tabular-nums">
                      {unit.conversion ?? "—"}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => toast.info(`Редактирование ${unit.symbol}`)}
                      >
                        Изменить
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Panel>
        </TabsContent>
        <TabsContent value="materials">
          <Panel title="Иерархия материалов">
            <div className="p-5">
              {dictionary.data.materials.map((material) => (
                <div key={material.id}>
                  <div className="border-l-[3px] border-primary px-3 py-2">
                    {material.name}
                  </div>
                  {material.children?.map((child) => (
                    <div key={child.id}>
                      <div className="ml-5 border-l-[3px] border-primary px-3 py-2">
                        {child.name}
                      </div>
                      {child.children?.map((leaf) => (
                        <div
                          className="ml-10 border-l-[3px] border-[#F26419] px-3 py-2"
                          key={leaf.id}
                        >
                          {leaf.name}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ))}
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={() => toast.info("Форма материала открыта")}
              >
                <Plus />
                Добавить материал
              </Button>
            </div>
          </Panel>
        </TabsContent>
        <TabsContent value="templates">
          <Panel title="Шаблоны параметров">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Тип оборудования</TableHead>
                  <TableHead>Параметры</TableHead>
                  <TableHead>Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {admin.data.templates.map((template) => (
                  <TableRow key={template.id}>
                    <TableCell>{template.equipment}</TableCell>
                    <TableCell>{template.parameters.join(", ")}</TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => toast.info("Шаблон открыт")}
                      >
                        Изменить
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Panel>
        </TabsContent>
      </Tabs>
    </>
  );
}

function ThresholdField({
  label,
  hint,
  color,
  input,
}: {
  label: string;
  hint: string;
  color: string;
  input: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="threshold-item">
          <div className="flex items-center justify-between gap-3">
            <div className="threshold-label" style={{ color }}>
              {label}
            </div>
            {input}
          </div>
          <div className="threshold-desc">{hint}</div>
        </div>
      </TooltipTrigger>
      <TooltipContent>
        <span className="flex items-center gap-2">
          <Info className="size-3" />
          {hint}
        </span>
      </TooltipContent>
    </Tooltip>
  );
}
