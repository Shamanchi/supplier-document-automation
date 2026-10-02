import { FileText, Info, Play, Search, Square, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { OrderStatusBadge, Progress, SourceTag } from "@/entities/order/ui";
import { useAuth } from "@/features/auth/model";
import {
  useCancelOrder,
  useDeleteOrder,
  useOrder,
  useOrderCommercials,
  useRunOrder,
} from "@/shared/api/hooks";
import { toPriceCheckInput, toQuoteInput } from "@/shared/api/mappers";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { ErrorState, LoadingState } from "@/shared/ui/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import { BatchVerdictPanel } from "@/widgets/order-detail/batch-verdict";
import {
  BatchKitStatusBadge,
  BitrixImportButton,
} from "@/widgets/order-detail/bitrix";
import { OrderItems } from "@/widgets/order-detail/order-items";
import { PriceCheck } from "@/widgets/order-detail/price-check";
import { PriceUploadButton } from "@/widgets/order-detail/price-upload";
import { Quote } from "@/widgets/order-detail/quote";

function formatMoment(value: string) {
  return new Date(value).toLocaleString("ru-RU");
}

/** Пустое состояние вкладки контура: данных нет, но экран живой. */
function ContourEmptyState({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 p-5 text-sm text-muted-foreground">
      <Info className="size-4" />
      {text}
    </div>
  );
}

export function OrderDetailPanel({ orderId }: { orderId: string }) {
  const { data: order, isLoading, isError } = useOrder(orderId);
  // Контур КП (задача №7): расчётный лист заявки — данные вкладок
  // цены/КП. Лист — производная заявки (1:1), но грузится отдельным
  // запросом: сбой в нём не должен ронять страницу заявки.
  const { data: commercials, isLoading: commercialsLoading } =
    useOrderCommercials(orderId);
  const runOrder = useRunOrder();
  const cancelOrder = useCancelOrder();
  const deleteOrder = useDeleteOrder();
  const { isAdminLike, user } = useAuth();
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (isLoading) return <LoadingState label="Загрузка заявки..." />;
  if (isError || !order) return <ErrorState label="Не удалось открыть заявку" />;

  const canWrite = isAdminLike || user?.roles.includes("manager") === true;

  // Закрытая/архивная заявка: контуры цены и КП недоступны для правки,
  // показывается итоговый баннер (ветка рендера из P2-версии виджета).
  if (order.status === "closed" || order.status === "archived") {
    const priceInput = toPriceCheckInput(order, commercials ?? null);
    const quoteInput = toQuoteInput(order, commercials ?? null);
    return (
      <div className="accordion-content open p-5">
        <div
          className={
            order.status === "closed" ? "closed-banner" : "archived-banner"
          }
        >
          <Info className="size-5" />
          Заявка {order.number}{" "}
          {order.status === "closed" ? "закрыта" : "находится в архиве"}. Итог:
          вердикт «{order.finalVerdict}»
        </div>
        <Tabs defaultValue="items">
          <TabsList>
            <TabsTrigger value="items">
              <FileText />
              Позиции и документы
            </TabsTrigger>
            <TabsTrigger value="price">
              <Search />
              Проверка цен
            </TabsTrigger>
            <TabsTrigger value="quote">
              <FileText />
              КП
            </TabsTrigger>
          </TabsList>
          <TabsContent value="items">
            <div className="flex flex-col gap-4">
              <OrderItems order={order} />
              <BatchVerdictPanel canWrite={false} orderId={order.id} tenderId={order.tenderId} />
            </div>
          </TabsContent>
          <TabsContent value="price">
            {commercialsLoading ? (
              <LoadingState label="Загрузка расчётного листа..." />
            ) : priceInput.files.length || priceInput.pricePositions.length ? (
              <PriceCheck order={priceInput} />
            ) : (
              <ContourEmptyState text="Нет позиций для подбора цен" />
            )}
          </TabsContent>
          <TabsContent value="quote">
            {quoteInput.quotePositions.length ? (
              <Quote order={quoteInput} />
            ) : (
              <ContourEmptyState text="Итоговое КП заявки не сформировано" />
            )}
          </TabsContent>
        </Tabs>
      </div>
    );
  }

  const start = async () => {
    const result = await runOrder.mutateAsync(order.id);
    if (result.started.length) {
      toast.success(`Позиций отправлено на проверку: ${result.started.length}`);
    }
    // Отклонённые приходят вместе с принятыми: пакетный запуск в режиме
    // READY_ITEMS_ONLY не падает целиком из-за одной неготовой позиции.
    for (const rejection of result.rejected) {
      toast.warning(rejection.message);
    }
    if (!result.started.length && !result.rejected.length) {
      toast.info("Запускать нечего: все позиции уже в работе");
    }
  };

  const stop = async () => {
    await cancelOrder.mutateAsync(order.id);
    toast.success("Запрошена отмена проверки");
  };

  // Контуры Фазы 2 читают свою P2-модель; адаптеры собирают её из заявки
  // Фазы 3 и её расчётного листа без выдумывания данных (пустые списки =
  // пустые состояния — расчёт/прайс ещё не созданы).
  const priceInput = toPriceCheckInput(order, commercials ?? null);
  const quoteInput = toQuoteInput(order, commercials ?? null);

  const remove = async () => {
    await deleteOrder.mutateAsync(order.id);
    setConfirmDelete(false);
    toast.success(`Черновик ${order.number} удалён`);
  };

  return (
    <div className="accordion-content open p-5">
      <div className="mb-3">
        <div className="flex-between mb-2">
          <div className="button-row">
            <strong className="text-base">Заявка №{order.number}</strong>
            <OrderStatusBadge
              status={order.status}
              statusLabel={order.statusLabel}
            />
            <BatchKitStatusBadge linked={Boolean(order.tenderId)} orderId={order.id} />
            <Progress value={order.progress} />
          </div>
          {canWrite ? (
            <div className="button-row">
              <PriceUploadButton canWrite={canWrite} order={order} />
              <BitrixImportButton canWrite={canWrite} order={order} />
              {order.status === "draft" || order.status === "active" ? (
                <Button
                  disabled={runOrder.isPending}
                  onClick={start}
                  size="sm"
                >
                  <Play />
                  Запустить проверку заявки
                </Button>
              ) : null}
              {order.status === "active" ? (
                <Button
                  disabled={cancelOrder.isPending}
                  onClick={stop}
                  size="sm"
                  variant="destructive"
                >
                  <Square />
                  Отменить проверку заявки
                </Button>
              ) : null}
              {order.status === "draft" ? (
                <Button
                  onClick={() => setConfirmDelete(true)}
                  size="sm"
                  variant="outline"
                >
                  <Trash2 />
                  Удалить черновик
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="order-meta">
          <div>
            <span className="text-muted-foreground">Клиент:</span>{" "}
            {order.client}
          </div>
          <div>
            <span className="text-muted-foreground">Поставщик:</span>{" "}
            {order.supplier ?? "—"}
          </div>
          <div>
            <span className="text-muted-foreground">Создал:</span>{" "}
            {order.author}
          </div>
          <div>
            <span className="text-muted-foreground">Источник:</span>{" "}
            <SourceTag tenderId={order.tenderId} />
          </div>
          <div>
            <span className="text-muted-foreground">Создана:</span>{" "}
            {formatMoment(order.date)}
          </div>
          <div>
            <span className="text-muted-foreground">Изменена:</span>{" "}
            {formatMoment(order.updatedAt)}
          </div>
        </div>
      </div>

      <Tabs defaultValue="items">
        <TabsList>
          <TabsTrigger value="items">
            <FileText />
            Позиции и документы
          </TabsTrigger>
          <TabsTrigger value="price">
            <Search />
            Проверка цен
          </TabsTrigger>
          <TabsTrigger value="quote">
            <FileText />
            КП
          </TabsTrigger>
        </TabsList>
        <TabsContent value="items">
          <div className="flex flex-col gap-4">
            <OrderItems order={order} />
            <BatchVerdictPanel canWrite={canWrite} orderId={order.id} tenderId={order.tenderId} />
          </div>
        </TabsContent>
        <TabsContent value="price">
          {commercialsLoading ? (
            <LoadingState label="Загрузка расчётного листа..." />
          ) : priceInput.files.length || priceInput.pricePositions.length ? (
            <PriceCheck order={priceInput} />
          ) : (
            <ContourEmptyState text="Нет позиций для подбора цен" />
          )}
        </TabsContent>
        <TabsContent value="quote">
          {quoteInput.quotePositions.length ? (
            <Quote order={quoteInput} />
          ) : (
            <ContourEmptyState text="КП появится после создания расчёта по заявке в контуре Фазы 2 (заявка пока не связана с расчётом)" />
          )}
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        confirmLabel="Удалить"
        description="Черновик заявки будет удалён без возможности восстановления."
        destructive
        icon={<Trash2 />}
        onConfirm={remove}
        onOpenChange={setConfirmDelete}
        open={confirmDelete}
        pending={deleteOrder.isPending}
        title="Удалить черновик"
      />
    </div>
  );
}
