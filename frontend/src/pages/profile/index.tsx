import {
  Check,
  Clock3,
  FolderOpen,
  RotateCcw,
  Save,
  Settings,
  X,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { useAuth } from "@/features/auth/model";
import {
  useChangeRole,
  useNotificationDecision,
  useProfile,
  useSavePermissions,
} from "@/shared/api/hooks";
import type { Role } from "@/shared/model/types";
import { Button } from "@/shared/ui/button";
import { Checkbox } from "@/shared/ui/checkbox";
import { ErrorState, LoadingState, PageHeader, Panel } from "@/shared/ui/page";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

const roleNames: Record<Role, string> = {
  viewer: "Наблюдатель",
  admin: "Администратор",
  manager: "Менеджер",
  sysadmin: "Сисадмин",
};

const allPermissions = [
  "Создание заявок",
  "Загрузка файлов",
  "Ручной ввод параметров",
  "Предложение синонимов",
  "Управление поставщиками",
  "Управление товарами",
  "Подтверждение синонимов",
  "Управление пользователями",
  "Просмотр логов",
];

export function ProfilePage() {
  const navigate = useNavigate();
  const { role, setRole } = useAuth();
  const { data, isLoading, isError } = useProfile();
  const changeRole = useChangeRole();
  const notificationDecision = useNotificationDecision();
  const savePermissions = useSavePermissions();
  const [permissionRole, setPermissionRole] = useState<Role>("manager");
  const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);

  useEffect(() => {
    if (data) {
      setSelectedPermissions(data.permissions[permissionRole]);
    }
  }, [data, permissionRole]);

  const pendingNotifications = useMemo(
    () => data?.notifications.filter((item) => item.status === "pending") ?? [],
    [data],
  );

  if (isLoading) return <LoadingState />;
  if (isError || !data) return <ErrorState />;

  const switchRole = async (nextRole: Role) => {
    await changeRole.mutateAsync(nextRole);
    setRole(nextRole);
    toast.success(`Роль переключена: ${roleNames[nextRole]}`);
    if (nextRole === "manager" && location.pathname === "/management") {
      navigate("/profile");
    }
  };

  return (
    <>
      <PageHeader
        title="Профиль"
        subtitle="Учётная запись, роль и демонстрация доступов"
      />
      <div className="max-w-[680px]">
        <Panel>
          <div className="p-6">
            <div className="mb-5 flex items-center gap-4">
              <div className="flex size-14 items-center justify-center rounded-full bg-primary text-xl font-semibold text-white">
                {data.initials}
              </div>
              <div>
                <div className="text-lg font-semibold">{roleNames[role]}</div>
                <div className="text-sm text-muted-foreground">{data.email}</div>
              </div>
            </div>

            {role === "sysadmin" ? (
              <div className="border-t border-border py-3">
                <Button asChild variant="outline">
                  <a href="/engineering/">
                    <Wrench />
                    Открыть инженерный интерфейс
                  </a>
                </Button>
              </div>
            ) : null}
            <section className="border-t border-border py-3">
              <div className="mb-2 flex items-center gap-2 font-semibold">
                <FolderOpen className="size-4" />
                Уведомления
              </div>
              {pendingNotifications.length ? (
                <div className="grid gap-2">
                  {pendingNotifications.map((notification) => (
                    <div
                      className="rounded-md border-l-[3px] border-[#F26419] bg-primary/5 px-3 py-2 text-sm"
                      key={notification.id}
                    >
                      <div className="flex flex-wrap justify-between gap-2">
                        <span className="flex items-center gap-2">
                          <Clock3 className="size-4" />
                          {notification.title}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {notification.createdAt}
                        </span>
                      </div>
                      <div className="button-row mt-2">
                        <Button
                          size="sm"
                          onClick={async () => {
                            await notificationDecision.mutateAsync({
                              notificationId: notification.id,
                              status: "approved",
                            });
                            toast.success("Изменение подтверждено");
                          }}
                        >
                          <Check />
                          Подтвердить
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={async () => {
                            await notificationDecision.mutateAsync({
                              notificationId: notification.id,
                              status: "rejected",
                            });
                            toast.success("Изменение отклонено");
                          }}
                        >
                          <X />
                          Отклонить
                        </Button>
                        <Button
                          variant="link"
                          onClick={() => navigate("/orders")}
                        >
                          <FolderOpen />
                          Открыть заявку
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">
                  Нет новых уведомлений
                </div>
              )}
            </section>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border py-3">
              <span className="text-muted-foreground">Текущая роль</span>
              <strong className="text-[#F26419]">{roleNames[role]}</strong>
            </div>
            <section className="border-t border-border py-3">
              <span className="mb-2 block text-sm text-muted-foreground">
                Переключить роль (демонстрация)
              </span>
              <div className="role-switch">
                {(Object.keys(roleNames) as Role[]).map((item) => (
                  <button
                    type="button"
                    className={role === item ? "active" : undefined}
                    key={item}
                    onClick={() => void switchRole(item)}
                  >
                    {roleNames[item]}
                  </button>
                ))}
              </div>
            </section>

            {role === "sysadmin" ? (
              <section className="border-t border-border py-3">
                <div className="mb-2 flex items-center gap-2 font-semibold">
                  <Settings className="size-4" />
                  Управление правами
                </div>
                <Select
                  value={permissionRole}
                  onValueChange={(value) => setPermissionRole(value as Role)}
                >
                  <SelectTrigger className="mb-2 w-52">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="manager">Менеджер</SelectItem>
                    <SelectItem value="admin">Администратор</SelectItem>
                    <SelectItem value="sysadmin">Сисадмин</SelectItem>
                  </SelectContent>
                </Select>
                <div className="permission-group">
                  {allPermissions.map((permission, index) => {
                    const mandatory = index < 3;
                    const checked = selectedPermissions.includes(permission);
                    return (
                      <label className="perm-item" key={permission}>
                        <Checkbox
                          checked={mandatory || checked}
                          disabled={mandatory}
                          onCheckedChange={(value) => {
                            setSelectedPermissions((current) =>
                              value
                                ? [...new Set([...current, permission])]
                                : current.filter((item) => item !== permission),
                            );
                          }}
                        />
                        <span className="perm-label">{permission}</span>
                        <span className="perm-role-tag">
                          {mandatory ? "все роли" : roleNames[permissionRole]}
                        </span>
                      </label>
                    );
                  })}
                </div>
                <div className="button-row mt-2">
                  <Button
                    size="sm"
                    onClick={async () => {
                      await savePermissions.mutateAsync({
                        role: permissionRole,
                        permissions: selectedPermissions,
                      });
                      toast.success("Настройки прав сохранены");
                    }}
                  >
                    <Save />
                    Сохранить
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setSelectedPermissions(data.permissions[permissionRole])
                    }
                  >
                    <RotateCcw />
                    Сбросить
                  </Button>
                </div>
              </section>
            ) : null}

            <section className="border-t border-border py-3">
              <span className="mb-2 block text-sm text-muted-foreground">
                Доступные действия для роли:
              </span>
              <ul className="grid list-none gap-1 p-0 text-sm">
                {data.permissions[role].map((permission) => (
                  <li className="flex items-center gap-2" key={permission}>
                    <Check className="size-4 text-green-600" />
                    {permission}
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </Panel>
      </div>
    </>
  );
}
