import { FolderOpen, Plus, Search, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useFieldArray, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { OrderStatusBadge, Progress, SourceTag } from "@/entities/order/ui";
import { useCreateOrder, useOrders } from "@/shared/api/hooks";
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
import { ErrorState, LoadingState, PageHeader, Panel } from "@/shared/ui/page";
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
import { OrderDetailPanel } from "@/widgets/order-detail/order-detail";

/** Предел позиций в пакете — ограничение схемы (`ck_item_position_range`). */
const MAX_ITEMS = 5;

const createOrderSchema = z.object({
  customer: z.string().min(3, "Укажите клиента"),
  title: z.string().optional(),
  supplier: z.string().optional(),
  tenderId: z.string().optional(),
  items: z
    .array(z.object({ name: z.string().min(2, "Укажите оборудование") }))
    .min(1)
    .max(MAX_ITEMS),
});

type CreateOrderValues = z.infer<typeof createOrderSchema>;

export function OrdersPage() {
  const [status, setStatus] = useState("all");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [openOrders, setOpenOrders] = useState<string[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const { data, isLoading, isError } = useOrders({ status, search });
  const createOrder = useCreateOrder();
  const form = useForm<CreateOrderValues>({
    resolver: zodResolver(createOrderSchema),
    defaultValues: {
      customer: "",
      title: "",
      supplier: "",
      tenderId: "",
      items: [{ name: "" }],
    },
  });
  const items = useFieldArray({ control: form.control, name: "items" });

  const submitOrder = form.handleSubmit(async (values) => {
    try {
      const order = await createOrder.mutateAsync({
        customer: values.customer,
        // Пустые строки не отправляем: сервер отличает «не передали» от
        // «передали пустое», и пустая строка попала бы в реквизиты как есть.
        title: values.title || undefined,
        supplier: values.supplier || undefined,
        tenderId: values.tenderId || undefined,
        items: values.items,
      });
      toast.success(`Заявка ${order.number} создана как черновик`);
      form.reset();
      setCreateOpen(false);
      setOpenOrders((opened) => [...opened, order.id]);
    } catch (cause) {
      toast.error(
        cause instanceof Error ? cause.message : "Не удалось создать заявку",
      );
    }
  });

  return (
    <>
      <PageHeader
        title="Заявки (тендеры)"
        subtitle="Пакеты проверки: позиции, документы и прогоны"
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus />
            Создать заявку
          </Button>
        }
      />
      <Panel>
        <div className="filter-row">
          <div className="filter-group">
            <label>Фильтр по статусу</label>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="draft">Черновики</SelectItem>
                <SelectItem value="active">В работе</SelectItem>
                <SelectItem value="confirmation">На подтверждении</SelectItem>
                <SelectItem value="closed">Завершённые</SelectItem>
                <SelectItem value="archived">Архив</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="filter-group grow">
            <label htmlFor="order-search">Поиск</label>
            <Input
              id="order-search"
              placeholder="Название, клиент, Tender ID"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") setSearch(searchDraft);
              }}
            />
          </div>
          <Button size="sm" onClick={() => setSearch(searchDraft)}>
            <Search />
            Найти
          </Button>
        </div>
        {isLoading ? (
          <LoadingState />
        ) : isError || !data ? (
          <ErrorState />
        ) : data.length === 0 ? (
          <div className="p-5 text-sm text-muted-foreground">
            Заявок не найдено. Создайте первую — она появится как черновик.
          </div>
        ) : (
          <Table className="orders-table">
            <TableHeader>
              <TableRow>
                <TableHead>Заявка</TableHead>
                <TableHead>Клиент / Источник</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Прогресс</TableHead>
                <TableHead className="text-center">Действия</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((order) => {
                const isOpen = openOrders.includes(order.id);
                return (
                  <Fragment key={order.id}>
                    <TableRow className="order-summary-row">
                      <TableCell className="font-semibold">
                        {order.number}
                      </TableCell>
                      <TableCell>
                        <div>{order.client}</div>
                        <SourceTag tenderId={order.tenderId} />
                      </TableCell>
                      <TableCell>
                        <OrderStatusBadge
                          status={order.status}
                          statusLabel={order.statusLabel}
                        />
                      </TableCell>
                      <TableCell>
                        <Progress value={order.progress} />
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap justify-center gap-2">
                          <Button
                            variant="link"
                            onClick={() =>
                              setOpenOrders((opened) =>
                                isOpen
                                  ? opened.filter((id) => id !== order.id)
                                  : [...opened, order.id],
                              )
                            }
                          >
                            <FolderOpen />
                            {isOpen ? "Закрыть" : "Открыть"}
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                    {isOpen ? (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={5} className="order-detail-cell p-0">
                          <OrderDetailPanel orderId={order.id} />
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        )}
      </Panel>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <Plus />
              Новая заявка
            </DialogTitle>
            <DialogDescription>
              Заявка создаётся черновиком: документы позиций загружаются в
              карточке, проверка запускается оттуда же.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={submitOrder}>
            <div className="grid gap-2">
              <Label htmlFor="order-customer">Клиент</Label>
              <Input id="order-customer" {...form.register("customer")} />
              <span className="field-error">
                {form.formState.errors.customer?.message}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="order-title">Название заявки</Label>
              <Input
                id="order-title"
                placeholder="Необязательно: заменяет номер в списке"
                {...form.register("title")}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="order-supplier">Поставщик</Label>
              <Input id="order-supplier" {...form.register("supplier")} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="order-tender">Tender ID</Label>
              <Input id="order-tender" {...form.register("tenderId")} />
            </div>
            <div className="grid gap-2">
              <Label>Позиции (до {MAX_ITEMS})</Label>
              {items.fields.map((field, index) => (
                <div className="flex items-center gap-2" key={field.id}>
                  <Input
                    aria-label={`Позиция ${index + 1}`}
                    placeholder="Оборудование"
                    {...form.register(`items.${index}.name`)}
                  />
                  {items.fields.length > 1 ? (
                    <Button
                      onClick={() => items.remove(index)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <Trash2 />
                    </Button>
                  ) : null}
                </div>
              ))}
              <span className="field-error">
                {form.formState.errors.items?.[0]?.name?.message ??
                  form.formState.errors.items?.message}
              </span>
              {items.fields.length < MAX_ITEMS ? (
                <Button
                  className="justify-self-start"
                  onClick={() => items.append({ name: "" })}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <Plus />
                  Добавить позицию
                </Button>
              ) : null}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateOpen(false)}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={createOrder.isPending}>
                Создать
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
