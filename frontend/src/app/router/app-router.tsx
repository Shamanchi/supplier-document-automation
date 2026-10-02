import { lazy } from "react";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";

import { ManagementRoute } from "@/app/router/protected-route";
import { useAuth } from "@/features/auth/model";
import { env } from "@/shared/config/env";
import { LoadingState } from "@/shared/ui/page";
import { AppShell } from "@/widgets/app-shell/app-shell";

const CertificatesPage = lazy(() =>
  import("@/pages/certificates").then((module) => ({
    default: module.CertificatesPage,
  })),
);
const DashboardPage = lazy(() =>
  import("@/pages/dashboard").then((module) => ({
    default: module.DashboardPage,
  })),
);
const DictionaryPage = lazy(() =>
  import("@/pages/dictionary").then((module) => ({
    default: module.DictionaryPage,
  })),
);
const GoodsPage = lazy(() =>
  import("@/pages/goods").then((module) => ({
    default: module.GoodsPage,
  })),
);
const HistoryPage = lazy(() =>
  import("@/pages/history").then((module) => ({
    default: module.HistoryPage,
  })),
);
const ManagementPage = lazy(() =>
  import("@/pages/management").then((module) => ({
    default: module.ManagementPage,
  })),
);
const OrdersPage = lazy(() =>
  import("@/pages/orders").then((module) => ({
    default: module.OrdersPage,
  })),
);
const ProfilePage = lazy(() =>
  import("@/pages/profile").then((module) => ({
    default: module.ProfilePage,
  })),
);
const SuppliersPage = lazy(() =>
  import("@/pages/suppliers").then((module) => ({
    default: module.SuppliersPage,
  })),
);
const LoginPage = lazy(() =>
  import("@/pages/login").then((module) => ({
    default: module.LoginPage,
  })),
);

function AuthenticatedApp() {
  const { isAuthenticated, isLoading } = useAuth();
  if (isLoading) return <LoadingState label="Проверка сессии..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <AppShell />;
}

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: <AuthenticatedApp />,
    children: [
      {
        index: true,
        element: (
          <Navigate to={env.enableMsw ? "/dashboard" : "/suppliers"} replace />
        ),
      },
      { path: "dashboard", element: <DashboardPage /> },
      { path: "orders", element: <OrdersPage /> },
      { path: "suppliers", element: <SuppliersPage /> },
      { path: "goods", element: <GoodsPage /> },
      { path: "certificates", element: <CertificatesPage /> },
      { path: "dictionary", element: <DictionaryPage /> },
      { path: "history", element: <HistoryPage /> },
      {
        element: <ManagementRoute />,
        children: [{ path: "management", element: <ManagementPage /> }],
      },
      { path: "profile", element: <ProfilePage /> },
      { path: "*", element: <Navigate to="/dashboard" replace /> },
    ],
  },
]);

export function AppRouter() {
  return <RouterProvider router={router} />;
}
