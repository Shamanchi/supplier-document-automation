import { Suspense } from "react";
import {
  Archive,
  BookOpen,
  ClipboardCheck,
  FileBadge,
  FolderKanban,
  LayoutDashboard,
  LogOut,
  Moon,
  Settings,
  ShoppingBag,
  Sun,
  UserRound,
  UsersRound,
  Wrench,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { useTheme } from "@/app/providers/theme-provider";
import { useAuth } from "@/features/auth/model";
import { useProject } from "@/features/project/model";
import { useProfile } from "@/shared/api/hooks";
import { env } from "@/shared/config/env";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { LoadingState } from "@/shared/ui/page";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/shared/ui/tooltip";
import { Logo } from "@/widgets/app-shell/logo";

const navItems = [
  { to: "/dashboard", label: "Дашборд", icon: LayoutDashboard },
  { to: "/orders", label: "Заявки", icon: ClipboardCheck, badge: "5" },
  { to: "/suppliers", label: "Поставщики", icon: UsersRound },
  { to: "/goods", label: "Товары", icon: ShoppingBag },
  { to: "/certificates", label: "Техническая документация", icon: FileBadge },
  { to: "/dictionary", label: "Словарь", icon: BookOpen },
  { to: "/history", label: "История проверок", icon: Archive },
  { to: "/management", label: "Управление", icon: Settings, restricted: true },
];

export function AppShell() {
  const { theme, toggleTheme } = useTheme();
  const { role, canManageSystem } = useAuth();
  const { data: profile } = useProfile({ enabled: env.enableMsw });
  const notificationCount =
    role === "manager"
      ? 0
      : (profile?.notifications.filter((item) => item.status === "pending")
          .length ?? 0);

  return (
    <div className="app-layout">
      <aside className="app-sidebar">
        <Logo />
        <nav className="sidebar-nav" aria-label="Основная навигация">
          {navItems.map((item) => {
            const Icon = item.icon;
            const disabled = item.restricted && !canManageSystem;
            return (
              <Tooltip key={item.to}>
                <TooltipTrigger asChild>
                  <span>
                    <NavLink
                      to={disabled ? "#" : item.to}
                      onClick={(event) => {
                        if (disabled) event.preventDefault();
                      }}
                      className={({ isActive }) =>
                        cn(
                          "nav-item",
                          isActive && !disabled && "active",
                          disabled && "disabled",
                        )
                      }
                      aria-disabled={disabled}
                    >
                      <span className="nav-icon">
                        <Icon />
                      </span>
                      <span className="nav-label">{item.label}</span>
                      {item.badge ? (
                        <span className="nav-badge">{item.badge}</span>
                      ) : null}
                    </NavLink>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="right">
                  {disabled
                    ? `${item.label}: недоступно для роли Менеджер`
                    : item.label}
                </TooltipContent>
              </Tooltip>
            );
          })}
          <div className="sidebar-footer">
            <Tooltip>
              <TooltipTrigger asChild>
                {/*
                  Обёртка обязательна: `asChild` склеивает свой className с
                  дочерним через строку, а NavLink принимает функцию — она
                  превращалась в текст, и пункт оставался без класса `nav-item`
                  (тёмный цвет от `a { color: inherit }`, активное состояние не
                  подсвечивалось). Остальные пункты обёрнуты так же.
                */}
                <span>
                  <NavLink
                    to="/profile"
                    className={({ isActive }) =>
                      cn("nav-item notification-badge", isActive && "active")
                    }
                  >
                    <span className="nav-icon">
                      <UserRound />
                    </span>
                    <span className="nav-label">Профиль</span>
                    {notificationCount ? (
                      <span className="badge-dot">{notificationCount}</span>
                    ) : null}
                  </NavLink>
                </span>
              </TooltipTrigger>
              <TooltipContent side="right">Профиль</TooltipContent>
            </Tooltip>
          </div>
        </nav>
      </aside>

      <main className="app-main">
        {!env.enableMsw ? <LiveSessionBar /> : null}
        <Suspense fallback={<LoadingState label="Загрузка раздела..." />}>
          <Outlet />
        </Suspense>
      </main>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="icon"
            className="theme-toggle"
            onClick={toggleTheme}
            aria-label={
              theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему"
            }
          >
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {theme === "dark" ? "Светлая тема" : "Тёмная тема"}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

function LiveSessionBar() {
  const { user, role, logout } = useAuth();
  const { project, projects, selectProject } = useProject();

  return (
    <div className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
      <div className="flex min-w-0 items-center gap-2">
        <FolderKanban className="size-4 shrink-0 text-primary" />
        <Select value={project.id} onValueChange={selectProject}>
          <SelectTrigger className="w-[min(320px,70vw)]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {projects
              .filter((item) => !item.is_archived)
              .map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.name}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">{user?.email}</span>
        {role === "sysadmin" ? (
          <Button asChild type="button" variant="outline" size="sm">
            <a href="/engineering/">
              <Wrench />
              Инженерный UI
            </a>
          </Button>
        ) : null}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void logout()}
        >
          <LogOut />
          Выйти
        </Button>
      </div>
    </div>
  );
}
