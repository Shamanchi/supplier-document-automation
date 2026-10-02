import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  login as loginRequest,
  logout as logoutRequest,
  fetchCurrentUser,
  refreshSession,
  setAuthToken,
} from "@/shared/api/client";
import type { BackendUser } from "@/shared/api/backend-types";
import { mapBackendUserRole } from "@/shared/api/mappers";
import { env } from "@/shared/config/env";
import type { Role } from "@/shared/model/types";

interface AuthContextValue {
  user: BackendUser | null;
  role: Role;
  setRole: (role: Role) => void;
  isAuthenticated: boolean;
  isLoading: boolean;
  isAdminLike: boolean;
  canManageSystem: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);
const MOCK_USER: BackendUser = {
  id: "demo-admin",
  email: "demo-admin@example.test",
  display_name: "Demo Admin",
  initials: "DA",
  roles: ["admin"],
  role: "admin",
  permissions: {},
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<BackendUser | null>(
    env.enableMsw ? MOCK_USER : null,
  );
  const [role, setRoleState] = useState<Role>("admin");
  const [isLoading, setIsLoading] = useState(!env.enableMsw);

  const applySession = (session: Awaited<ReturnType<typeof refreshSession>>) => {
    setAuthToken(session.accessToken);
  };

  const clearSession = () => {
    setAuthToken(null);
    setUser(null);
    setRoleState("manager");
  };

  useEffect(() => {
    if (env.enableMsw) return;
    refreshSession()
      .then(async (session) => {
        applySession(session);
        const currentUser = await fetchCurrentUser(session.accessToken);
        setUser(currentUser);
        setRoleState(mapBackendUserRole(currentUser.role, currentUser.roles));
      })
      .catch(clearSession)
      .finally(() => setIsLoading(false));
  }, []);

  useEffect(() => {
    if (env.enableMsw) return;
    const expire = () => clearSession();
    window.addEventListener("price-loader-auth-expired", expire);
    return () => window.removeEventListener("price-loader-auth-expired", expire);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      role,
      setRole: (nextRole) => {
        if (env.enableMsw) setRoleState(nextRole);
      },
      isAuthenticated: user !== null,
      isLoading,
      isAdminLike: role === "admin" || role === "sysadmin",
      canManageSystem: role !== "manager",
      login: async (email, password) => {
        const session = await loginRequest(email, password);
        applySession(session);
        const currentUser = await fetchCurrentUser(session.accessToken);
        setUser(currentUser);
        setRoleState(mapBackendUserRole(currentUser.role, currentUser.roles));
      },
      logout: async () => {
        await logoutRequest().catch(() => undefined);
        clearSession();
      },
    }),
    [user, role, isLoading],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
