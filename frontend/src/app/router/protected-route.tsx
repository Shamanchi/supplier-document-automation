import { Navigate, Outlet } from "react-router-dom";
import { toast } from "sonner";

import { useAuth } from "@/features/auth/model";

export function ManagementRoute() {
  const { canManageSystem } = useAuth();
  if (!canManageSystem) {
    toast.error("Раздел «Управление» недоступен для роли Менеджер");
    return <Navigate to="/profile" replace />;
  }
  return <Outlet />;
}
